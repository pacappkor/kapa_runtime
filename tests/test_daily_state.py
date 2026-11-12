# SPDX-License-Identifier: GPL-3.0-only
from dataclasses import replace
import json
import os
from pathlib import Path
import re
from types import SimpleNamespace

import pytest
import yaml
from huggingface_hub import CommitOperationAdd

from kapa_runtime import daily, state_bundle as b
from kapa_runtime.config import RuntimeSettings, ConfigurationError
from kapa_runtime.credentials import load_secret, redact, diagnose
from kapa_runtime.releases import owners
from kapa_runtime.releases.hf import _HubWriter, PublishError
from kapa_runtime.credentials import Secret
from kapa_runtime.storage.state import StateStore
from test_public_promotion import MultiRepoApi
from test_accumulation import setup, client, NOW
from test_batch_publish import publication


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv('KAPA_TEST_KEY', 'TESTKEY-not-real')
    monkeypatch.setenv('KAPA_TEST_HF', 'TESTHF-not-real')
    settings = RuntimeSettings(tmp_path, tmp_path / 'local', hf_namespace='owner',
        hf_repos={'state': 'state'}, credential_env={'data_go_kr': 'KAPA_TEST_KEY', 'hugging_face': 'KAPA_TEST_HF'})
    with StateStore(settings.state_db):
        pass
    (settings.data_root / 'operator-policy.json').write_text('{}')
    api = MultiRepoApi()
    api.create_repo('owner/state', private=True)
    return settings, api


def copy_settings(settings, name):
    return replace(settings, data_root=settings.data_root.parent / name)


def test_pull_daily_push_roundtrip_and_stop(state, monkeypatch):
    settings, api = state
    b.push(settings, initial=True, api=api)
    restored = copy_settings(settings, 'runner')
    b.pull(restored, api=api)
    monkeypatch.setattr(daily.accumulation, 'plan', lambda *a, **k: {})
    ids = []
    def collect(*args, job_id, **kwargs):
        ids.append(job_id)
        return {'state': 'paused'}
    monkeypatch.setattr(daily.accumulation, 'collect_batch', collect)
    assert daily.daily_run(restored, clock=lambda: NOW)['exit_code'] == 0
    b.push(restored, api=api)
    tomorrow = copy_settings(settings, 'tomorrow')
    b.pull(tomorrow, api=api)
    assert daily.daily_run(tomorrow, clock=lambda: NOW)['state'] == 'paused'
    assert ids[0] == ids[1]
    monkeypatch.setattr(daily.accumulation, 'collect_batch', lambda *a, **k: {'state': 'blocked'})
    assert daily.daily_run(tomorrow)['exit_code'] == 1
    b.push(tomorrow, api=api)
    later = copy_settings(settings, 'later')
    b.pull(later, api=api)
    assert daily.daily_run(later)['exit_code'] == 4
    daily.clear_stop(later)
    assert (later.data_root / 'daily/pending-job.txt').exists()
    assert not (later.data_root / 'daily/STOP').exists()


def test_daily_success_and_query_mismatch(state, monkeypatch):
    settings, _ = state
    monkeypatch.setattr(daily.accumulation, 'plan', lambda *a, **k: {})
    monkeypatch.setattr(daily.accumulation, 'collect_batch', lambda *a, **k: {'state': 'validated'})
    monkeypatch.setattr(daily, 'publish_batch', lambda *a, **k: {'state': 'published'})
    monkeypatch.setattr(daily.p, 'activate', lambda *a, **k: {'state': 'active'})
    monkeypatch.setattr(daily.p, 'query', lambda *a, **k: {'snapshot_id': 'daily-20261112-120000'})
    assert daily.daily_run(settings, clock=lambda: NOW)['state'] == 'active'
    assert not (settings.data_root / 'daily/pending-job.txt').exists()
    monkeypatch.setattr(daily.p, 'query', lambda *a, **k: {'snapshot_id': 'wrong'})
    assert daily.daily_run(settings, clock=lambda: NOW)['state'] == 'failed'


def test_concurrent_pushes_refuse_second(state):
    settings, api = state
    b.push(settings, initial=True, api=api)
    one, two = (copy_settings(settings, n) for n in ('one', 'two'))
    b.pull(one, api=api)
    b.pull(two, api=api)
    (one.data_root / 'operator-policy.json').write_text('{"changed":true}')
    b.push(one, api=api)
    with pytest.raises(ValueError, match='parent_changed'):
        b.push(two, api=api)


def test_commit_parent_closes_race(state, monkeypatch):
    settings, api = state
    b.push(settings, initial=True, api=api)
    (settings.data_root / 'operator-policy.json').write_text('{"changed":true}')
    original = api.create_commit
    def race(repo, **kwargs):
        original(repo, operations=[CommitOperationAdd(path_in_repo='race', path_or_fileobj=b'x')])
        if kwargs.get('parent_commit') != api.repos[repo]['sha']:
            raise ValueError('expected parent mismatch')
        return original(repo, **kwargs)
    monkeypatch.setattr(api, 'create_commit', race)
    with pytest.raises(Exception):
        b.push(settings, api=api)


def mutate_index(api, change):
    tree = api.repos['owner/state']['files']
    index = json.loads(tree[b.INDEX])
    change(index)
    index['digest'] = b.digest(b.encoded(index['files']))
    api.create_commit('owner/state', operations=[CommitOperationAdd(path_in_repo=b.INDEX, path_or_fileobj=b.encoded(index))])


def test_corrupt_bundle(state):
    settings, api = state
    b.push(settings, initial=True, api=api)
    files = api.repos['owner/state']['files']
    blob = next(n for n in files if n.startswith('blobs/'))
    api.create_commit('owner/state', operations=[CommitOperationAdd(path_in_repo=blob, path_or_fileobj=b'bad')])
    target = copy_settings(settings, 'corrupt')
    with pytest.raises(ValueError, match='corrupt'):
        b.pull(target, api=api)
    assert not target.state_db.exists()
    assert not (target.data_root / b.RECEIPT).exists()


@pytest.mark.parametrize('name,size', [('../escape', 1), ('state/../../escape', 1), ('state/x', b.MAX_FILE + 1), ('state/X', -1)])
def test_traversal_and_oversize(state, name, size):
    settings, api = state
    b.push(settings, initial=True, api=api)
    mutate_index(api, lambda index: index['files'].update({name: {'sha256': 'a' * 64, 'size': size}}))
    with pytest.raises(ValueError):
        b.pull(copy_settings(settings, 'invalid'), api=api)


def test_index_total_limit_and_duplicate():
    files = {'state/runtime.sqlite3': {'sha256': 'a' * 64, 'size': 1},
             'operator-policy.json': {'sha256': 'a' * 64, 'size': 1}}
    files.update({f'staging/{i}': {'sha256': 'a' * 64, 'size': b.MAX_FILE} for i in range(9)})
    with pytest.raises(ValueError, match='oversize'):
        b.validate_index(b.encoded({'format': 'kapa-state/1', 'files': files, 'digest': b.digest(b.encoded(files))}))
    with pytest.raises(ValueError):
        b.validate_index(b'{"format":1,"format":1}')


def test_incremental_and_sqlite_backup(state):
    settings, api = state
    b.push(settings, initial=True, api=api)
    count = len(api.commits)
    b.push(settings, api=api)
    assert len(api.commits) == count
    index = json.loads(api.repos['owner/state']['files'][b.INDEX])
    assert not any(n.startswith('snapshots/') or n.endswith('.lock') for n in index['files'])
    with pytest.raises(ValueError, match='already_initialized'):
        b.push(settings, initial=True, api=api)


def test_public_state_refused(state):
    settings, api = state
    api.repos['owner/state']['private'] = False
    with pytest.raises(PublishError, match='visibility'):
        b.push(settings, initial=True, api=api)
    with pytest.raises(ConfigurationError):
        replace(settings, hf_visibilities={'state': 'public'})


def test_snapshot_reconstruction_and_frozen_pending(publication, monkeypatch):
    settings, api = publication
    api.create_repo('owner/state', private=True)
    settings = replace(settings, hf_repos={**settings.hf_repos, 'state': 'state'})
    writer = _HubWriter(api, 'owner', 'state', role='state', secret_values=lambda: [Secret('TESTKEY-not-real')])
    monkeypatch.setattr(b, '_writer', lambda *a: writer)
    b.push(settings, initial=True, api=api)
    restored = copy_settings(settings, 'restore-published')
    b.pull(restored, api=api)
    from kapa_runtime import pipeline as p
    for rid in ('A', 'B'):
        manifest = p._read_manifest(restored.manifests_dir / (rid + '.json'))
        p._verify_local(restored.snapshots_dir / rid, manifest)
    assert restored.active_manifest.read_bytes() == settings.active_manifest.read_bytes()
    assert not any(n.startswith('snapshots/') for n in json.loads(api.repos['owner/state']['files'][b.INDEX])['files'])


def test_env_credentials_and_scan(state):
    settings, api = state
    secret = load_secret('data_go_kr', settings)
    assert redact('TESTKEY-not-real', [secret]) == '***'
    assert 'TESTKEY' not in str(secret)
    assert diagnose('hugging_face_write', settings)['source'] == 'env'
    (settings.data_root / 'operator-policy.json').write_text('{"leak":"TESTKEY-not-real"}')
    with pytest.raises(PublishError, match='secret'):
        b.push(settings, initial=True, api=api)
    with pytest.raises(ConfigurationError):
        replace(settings, credential_files={'data_go_kr': Path('not-read')})


def test_posix_owner_ids(tmp_path, monkeypatch):
    monkeypatch.setattr(owners, 'os', SimpleNamespace(name='posix', geteuid=lambda: 1001))
    path = SimpleNamespace(stat=lambda: SimpleNamespace(st_uid=1001))
    assert owners.owner_ids(path) == frozenset({'uid:1001'})
    assert owners.current_owner_ids() == frozenset({'uid:1001'})


def test_workflow_contract():
    root = Path(__file__).parents[1]
    workflow = yaml.safe_load((root / '.github/workflows/accumulate.yml').read_text())
    assert workflow['permissions'] == {'contents': 'read'}
    assert workflow['concurrency'] == {'group': 'accumulate', 'cancel-in-progress': False}
    assert workflow['jobs']['collect']['needs'] == 'test'
    assert workflow['jobs']['collect']['environment'] == 'collector'
    for job in workflow['jobs'].values():
        assert job['if'] == "github.ref == 'refs/heads/main'"
        assert job['timeout-minutes'] > 0
        for step in job['steps']:
            if 'uses' in step:
                assert re.fullmatch(r'[^@]+@[a-f0-9]{40}', step['uses'])
    steps = workflow['jobs']['collect']['steps']
    assert steps[-1]['if'] == "always() && steps.pull.outcome == 'success'"
    assert 'state push' in steps[-1]['run']
    pins = json.loads((root / 'tools/collector-pins.json').read_text())
    assert all(re.fullmatch('[a-f0-9]{40}', pins[n]) for n in ('kapa_knowledge', 'kapa_skills'))


def test_bundle_refuses_symlinks(state, tmp_path):
    settings, api = state
    target = tmp_path / 'outside'
    target.write_bytes(b'not-state')
    link = settings.data_root / 'staging' / 'linked'
    link.parent.mkdir()
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip('symlink creation not permitted')
    with pytest.raises(ValueError, match='link'):
        b.push(settings, initial=True, api=api)


def test_daily_recovery_after_publication(state, monkeypatch):
    settings, _ = state
    root = settings.data_root / 'daily'
    root.mkdir()
    (root / 'pending-job.txt').write_text('published-job')
    with StateStore(settings.state_db) as store:
        store._connection.execute('INSERT INTO releases VALUES (?,?,?,?)',
                                 ('published-job', 'published', '[]', NOW.isoformat()))
    monkeypatch.setattr(daily.accumulation, 'collect_batch', lambda *a, **k: pytest.fail('recollected'))
    monkeypatch.setattr(daily, 'publish_batch', lambda *a, **k: {'state': 'published'})
    monkeypatch.setattr(daily.p, 'activate', lambda *a, **k: {'state': 'active'})
    monkeypatch.setattr(daily.p, 'query', lambda *a, **k: {'snapshot_id': 'published-job'})
    assert daily.daily_run(settings)['state'] == 'active'


def test_pending_policy_not_rebound(state, monkeypatch):
    settings, _ = state
    root = settings.data_root / 'daily'
    root.mkdir()
    (root / 'pending-job.txt').write_text('pending')
    original = (settings.data_root / 'operator-policy.json').read_bytes()
    monkeypatch.setattr(daily.p, '_git_revision', lambda *a: 'a' * 40)
    assert daily.daily_run(settings, fixture_revision='a' * 40)['state'] == 'failed'
    assert (settings.data_root / 'operator-policy.json').read_bytes() == original


@pytest.mark.parametrize('arguments,expected', [(['state', 'pull'], 'restored'),
                                                (['state', 'push', '--initial'], 'saved'),
                                                (['daily-run'], 'paused'),
                                                (['clear-stop'], 'cleared')])
def test_cli_commands(state, monkeypatch, capsys, arguments, expected):
    from kapa_runtime import cli
    settings, _ = state
    monkeypatch.setattr(cli.RuntimeSettings, 'load', lambda *a: settings)
    monkeypatch.setattr(b, 'pull', lambda *a: {'state': 'restored'})
    monkeypatch.setattr(b, 'push', lambda *a, **k: {'state': 'saved'})
    monkeypatch.setattr(daily, 'daily_run', lambda *a, **k: {'state': 'paused', 'exit_code': 0})
    assert cli.main(arguments) == 0
    assert json.loads(capsys.readouterr().out)['state'] == expected


def test_config_load_env_source(tmp_path):
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'workspace': '.', 'credential_env': {'data_go_kr': 'CUSTOM_KEY'},
                               'hf_namespace': 'owner', 'hf_repos': {'state': 'private-state'}}))
    settings = RuntimeSettings.load(path)
    assert settings.credential_env == {'data_go_kr': 'CUSTOM_KEY'}
    assert load_secret('data_go_kr', settings, {'CUSTOM_KEY': 'TESTKEY-not-real'}).reveal() == 'TESTKEY-not-real'
    with pytest.raises(Exception):
        load_secret('data_go_kr', settings, {'CUSTOM_KEY': ''})


def test_posix_explicit_trust_decision(tmp_path, monkeypatch):
    from kapa_runtime.releases import workspace
    repo = tmp_path / 'repo'
    (repo / '.git').mkdir(parents=True)
    monkeypatch.setattr(workspace, 'current_owner_ids', lambda: frozenset({'uid:1000'}))
    monkeypatch.setattr(workspace, 'owner_ids', lambda path: frozenset({'uid:1001'}))
    workspace._trusted_owner(repo, ('uid:1001',))
    with pytest.raises(workspace.WorkspaceError, match='untrusted_owner'):
        workspace._trusted_owner(repo, ())
