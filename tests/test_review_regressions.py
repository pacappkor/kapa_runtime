# SPDX-License-Identifier: GPL-3.0-only
import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from publication_fixture import seal_publication

from kapa_runtime import pipeline as p
from kapa_runtime.credentials import Secret
from kapa_runtime.releases.hf import _HubWriter, PublishError
from kapa_runtime.storage.state import StateStore
from test_hf_publish import FakeHfApi, FakeSecret
from test_pipeline import settings, http, collected, SimulatedProcessExit


@pytest.mark.parametrize('api', [FakeHfApi(), SimpleNamespace()])
def test_empty_scan_set_refused(api):
    with pytest.raises(PublishError, match='Nonempty secret scan set'):
        _HubWriter(api, 'owner', 'data', role='market')


@pytest.mark.parametrize('location', ['content', 'path', 'namespace'])
def test_injected_token_added_to_needles(tmp_path, location):
    api = FakeHfApi()
    api.token = 'hf_InjectedOnly12345'
    name = api.token + '.json' if location == 'path' else 'data.json'
    (tmp_path / name).write_text(api.token if location == 'content' else '{}')
    with pytest.raises(PublishError, match='configured secret'):
        publisher = _HubWriter(api, api.token if location == 'namespace' else 'owner', 'data',
                                role='market', secret_values=lambda: [FakeSecret()])
        publisher.upload_candidate(tmp_path, [name])
    assert not api.commits


@pytest.mark.parametrize('value,padding', [('synthetic-a', 1), ('synthetic-abc', 2)])
@pytest.mark.parametrize('encoder', [base64.b64encode, base64.urlsafe_b64encode])
def test_unpadded_base64_needles(tmp_path, value, padding, encoder):
    encoded = encoder(value.encode())
    assert len(encoded) - len(encoded.rstrip(b'=')) == padding
    (tmp_path / 'data.json').write_bytes(encoded.rstrip(b'='))
    api = FakeHfApi()
    with pytest.raises(PublishError, match='configured secret'):
        _HubWriter(api, 'owner', 'data', role='market',
                    secret_values=lambda: [Secret(value)]).upload_candidate(tmp_path, ['data.json'])
    assert not api.commits


@pytest.mark.parametrize('window', ['draft', 'mkdir', 'metadata'])
def test_initial_creation_crash_retry(settings, monkeypatch, window):
    original_transition = StateStore.transition_release
    original_mkdir = Path.mkdir
    original_write = p._json_write
    def transition(self, release, state, evidence):
        result = original_transition(self, release, state, evidence)
        if state == 'draft' and window == 'draft':
            raise SimulatedProcessExit()
        return result
    def mkdir(path, *a, **k):
        result = original_mkdir(path, *a, **k)
        if path == settings.staging_dir / 'r1' and window == 'mkdir':
            raise SimulatedProcessExit()
        return result
    def write(path, value):
        result = original_write(path, value)
        if path.name == 'collection.json' and window == 'metadata':
            raise SimulatedProcessExit()
        return result
    monkeypatch.setattr(StateStore, 'transition_release', transition)
    monkeypatch.setattr(Path, 'mkdir', mkdir)
    monkeypatch.setattr(p, '_json_write', write)
    with http() as client, pytest.raises(SimulatedProcessExit):
        p.collect(settings, service=p.SERVICES[0], lawd_code='11110', deal_ymd='202510', release_id='r1', client=client)
    monkeypatch.setattr(StateStore, 'transition_release', original_transition)
    monkeypatch.setattr(Path, 'mkdir', original_mkdir)
    monkeypatch.setattr(p, '_json_write', original_write)
    with StateStore(settings.state_db) as store:
        assert store.get_release('r1')['state'] == 'draft'
    with http() as client:
        assert p.collect(settings, service=p.SERVICES[0], lawd_code='11110', deal_ymd='202510', release_id='r1', client=client)['state'] == 'collecting'


@pytest.mark.parametrize('orphan', ['matching', 'wrong_scope', 'wrong_id', 'missing'])
def test_legacy_orphan_recovery(settings, orphan):
    root = settings.staging_dir / 'r1'
    root.mkdir(parents=True)
    meta = {'release_id': 'other' if orphan == 'wrong_id' else 'r1', 'services': list(p.SERVICES),
            'lawd_code': '22220' if orphan == 'wrong_scope' else '11110', 'deal_ymd': '202510', 'completed': {}}
    if orphan != 'missing':
        (root / 'collection.json').write_text(json.dumps(meta))
    with http() as client:
        if orphan == 'matching':
            assert p.collect(settings, service=p.SERVICES[0], lawd_code='11110', deal_ymd='202510', release_id='r1', client=client)['state'] == 'collecting'
        else:
            with pytest.raises(p.PipelineError, match='release_scope_mismatch|recovery_metadata_invalid'):
                p.collect(settings, service=p.SERVICES[0], lawd_code='11110', deal_ymd='202510', release_id='r1', client=client)
            with StateStore(settings.state_db) as store:
                assert store.get_release('r1') is None


@pytest.mark.parametrize('state', ['collecting', 'failed', 'partial', 'quarantined'])
def test_query_refuses_nonready_release(settings, state):
    with http() as client:
        p.collect(settings, service=p.SERVICES[0], lawd_code='11110', deal_ymd='202510', release_id='r1', client=client)
    if state == 'quarantined':
        with http() as client:
            p.collect(settings, service=p.SERVICES[1], lawd_code='11110', deal_ymd='202510', release_id='r1', client=client)
    if state != 'collecting':
        with StateStore(settings.state_db) as store:
            store.transition_release('r1', state, {'reason': 'synthetic', 'limited_scope': {'service': p.SERVICES[0]}})
    with pytest.raises(p.PipelineError, match='query_release_state_not_ready'):
        p.query(settings, filters={}, release_id='r1')


@pytest.mark.parametrize('damage', ['none', 'missing_service', 'hash', 'rows'])
def test_normalized_query_verification(settings, damage):
    collected(settings)
    meta_path = settings.staging_dir / 'r1' / 'collection.json'
    meta = json.loads(meta_path.read_text())
    if damage == 'missing_service':
        del meta['completed'][p.SERVICES[0]]
    elif damage == 'hash':
        meta['completed'][p.SERVICES[0]]['files'][0]['sha256'] = '0' * 64
    elif damage == 'rows':
        meta['completed'][p.SERVICES[0]]['files'][0]['rows'] += 1
    meta_path.write_text(json.dumps(meta))
    if damage != 'none':
        with pytest.raises(p.PipelineError, match='query_release_output_unverified'):
            p.query(settings, filters={}, release_id='r1')
    else:
        result = p.query(settings, filters={}, release_id='r1')
        assert result['release_state'] == 'normalized'
        assert result['verification']['complete'] and result['verification']['rows_verified']
        assert result['verification']['services'] == list(p.SERVICES)


@pytest.mark.parametrize('public', [False, True])
@pytest.mark.parametrize('dry_run', [False, True])
def test_terms_basis_change_requires_rebuild(settings, public, dry_run):
    from dataclasses import replace
    settings = replace(settings, hf_visibilities={'market': 'public' if public else 'private'})
    collected(settings)
    p.build(settings, release_id='r1')
    policy_path = settings.data_root / 'operator-policy.json'
    policy = json.loads(policy_path.read_text())
    policy['services'][p.SERVICES[0]]['terms_basis'] = 'corrected synthetic terms'
    policy_path.write_text(json.dumps(policy))
    api = FakeHfApi(private=not public)
    with pytest.raises(p.PipelineError, match='terms_changed_rebuild_required'):
        p.publish(settings, release_id='r1', api=api, dry_run=dry_run)
    assert not api.commits


@pytest.mark.parametrize('failure', ['unavailable', 'visibility', 'hash', 'missing_revision', 'none'])
def test_rollback_reverifies_published_target(settings, monkeypatch, failure):
    collected(settings, 'r1')
    p.build(settings, release_id='r1')
    api = FakeHfApi()
    p.publish(settings, release_id='r1', api=api)
    transition = StateStore.transition_release
    def crash(self, release_id, state, evidence):
        if state == 'active':
            raise SimulatedProcessExit()
        return transition(self, release_id, state, evidence)
    monkeypatch.setattr(StateStore, 'transition_release', crash)
    with pytest.raises(SimulatedProcessExit):
        p.activate(settings, release_id='r1', api=api)
    monkeypatch.setattr(StateStore, 'transition_release', transition)
    collected(settings, 'r2')
    p.build(settings, release_id='r2')
    second = FakeHfApi()
    p.publish(settings, release_id='r2', api=second)
    p.activate(settings, release_id='r2', api=second)
    before = settings.active_manifest.read_bytes()
    if failure == 'visibility':
        api.private = False
    elif failure == 'hash':
        api.files['README.md'] = b'changed'
    elif failure == 'missing_revision':
        api.files.clear()
    elif failure == 'unavailable':
        def unavailable(*a, **k):
            raise OSError('synthetic unavailable')
        api.repo_info = unavailable
    if failure == 'none':
        assert p.rollback(settings, api=api)['release_id'] == 'r1'
        with StateStore(settings.state_db) as store:
            assert store.get_release('r1')['guard_evidence'][-1]['evidence']['repo_revisions_verified'] is True
    else:
        with pytest.raises(p.PipelineError, match='rollback_remote_verification_failed'):
            p.rollback(settings, api=api)
        assert settings.active_manifest.read_bytes() == before
        with StateStore(settings.state_db) as store:
            assert store.get_release('r1')['state'] == 'published'
            assert store.get_release('r2')['state'] == 'active'


@pytest.mark.parametrize('alias,env', [('rone', 'RONE_API_KEY'), ('vworld', 'VWORLD_API_KEY')])
def test_env_only_credentials_scanned_at_publication(settings, monkeypatch, alias, env):
    value = 'FAKE-env-only-credential-12345'
    collected(settings)
    policy_path = settings.data_root / 'operator-policy.json'
    policy = json.loads(policy_path.read_text())
    policy['services'][p.SERVICES[0]]['terms_basis'] = 'synthetic basis ' + value
    policy_path.write_text(json.dumps(policy))
    p.build(settings, release_id='r1')
    assert alias not in settings.credential_files
    monkeypatch.setenv(env, value)
    api = FakeHfApi()
    with pytest.raises(PublishError, match='configured secret'):
        p.publish(settings, release_id='r1', api=api)
    assert not api.commits


@pytest.mark.parametrize('env', ['RONE_API_KEY', 'VWORLD_API_KEY'])
@pytest.mark.parametrize('value', ['', 'invalid value'])
def test_invalid_env_only_credentials_fail_closed(settings, monkeypatch, env, value):
    monkeypatch.setenv(env, value)
    with pytest.raises(Exception):
        p._loaded_secrets(settings)


@pytest.mark.parametrize('code', ['query_release_state_not_ready', 'query_release_output_unverified', 'terms_changed_rebuild_required'])
def test_cli_preserves_fixed_pipeline_error_code(tmp_path, monkeypatch, capsys, code):
    from kapa_runtime.cli import main
    config = tmp_path / 'runtime.json'
    config.write_text('{"workspace":"."}')
    def fail(*a, **k):
        raise p.PipelineError(code)
    monkeypatch.setattr(p, 'query', fail)
    assert main(['query', '--config', str(config), '--lawd-code', '11110']) == 1
    assert json.loads(capsys.readouterr().out)['error_code'] == code


def test_normalization_guards_require_replay_not_just_row_count(settings, monkeypatch):
    original = StateStore.transition_release
    def crash(self, release_id, state, evidence):
        if state == 'normalized':
            raise SimulatedProcessExit()
        return original(self, release_id, state, evidence)
    monkeypatch.setattr(StateStore, 'transition_release', crash)
    with http() as client, pytest.raises(SimulatedProcessExit):
        for service in p.SERVICES:
            p.collect(settings, service=service, lawd_code='11110', deal_ymd='202510', release_id='r1', client=client)
    monkeypatch.setattr(StateStore, 'transition_release', original)
    raw = next((settings.staging_dir / 'r1' / 'raw' / p.SERVICES[0]).rglob('*.xml'))
    payload = raw.read_bytes()
    assert b'<floor>9</floor>' in payload
    raw.write_bytes(payload.replace(b'<floor>9</floor>', b'<floor>5</floor>'))
    with http() as client, pytest.raises(p.PipelineError, match='recovery_output_verification_failed'):
        p.collect(settings, service=p.SERVICES[1], lawd_code='11110', deal_ymd='202510', release_id='r1', client=client)
    with StateStore(settings.state_db) as store:
        row = store.get_release('r1')
        assert row['state'] == 'failed'
        assert not any(e['state'] == 'normalized' for e in row['guard_evidence'])


@pytest.mark.parametrize('artifact', ['completed', 'raw', 'data', 'output'])
def test_orphan_with_collection_artifacts_never_adopted(settings, artifact):
    root = settings.staging_dir / 'orphan'
    root.mkdir(parents=True)
    meta = {'release_id': 'orphan', 'services': list(p.SERVICES), 'lawd_code': '11110',
            'deal_ymd': '202510', 'completed': {p.SERVICES[0]: {}} if artifact == 'completed' else {}}
    (root / 'collection.json').write_text(json.dumps(meta))
    if artifact in ('raw', 'data'):
        (root / artifact).mkdir()
    elif artifact == 'output':
        (root / 'README.md').write_text('synthetic output')
    with http() as client, pytest.raises(p.PipelineError, match='orphan_collection_artifacts_not_adoptable'):
        p.collect(settings, service=p.SERVICES[0], lawd_code='11110', deal_ymd='202510', release_id='orphan', client=client)
    with StateStore(settings.state_db) as store:
        assert store.get_release('orphan') is None
        assert store.reserved_requests(p.SERVICES[0], '2026-11-11') == 0


@pytest.mark.parametrize('damage', ['corrupt', 'secret', 'none'])
def test_publish_checks_inherited_remote_bytes(settings, damage):
    import hashlib
    from kapa_runtime.releases.manifest import dump_manifest, load_manifest
    collected(settings, 'r1')
    p.build(settings, release_id='r1')
    first = FakeHfApi()
    p.publish(settings, release_id='r1', api=first)
    old_path = p._manifest_path(settings, 'r1')
    old = load_manifest(old_path)
    content = b'TESTKEY-not-real' if damage == 'secret' else b'known synthetic historic content'
    name = 'schemas/previous.json'
    # Trusted old evidence may predate the current credential scan set.
    old['files'].append({'path': name, 'sha256': hashlib.sha256(content).hexdigest(), 'rows': 0})
    old['files'].sort(key=lambda f: f['path'])
    dump_manifest(old, old_path)
    seal_publication(settings, old)
    collected(settings, 'r2')
    p.build(settings, release_id='r2')
    class RetainingApi(FakeHfApi):
        def create_commit(self, *a, **k):
            ref = super().create_commit(*a, **k)
            self.files[name] = b'modified inherited bytes' if damage == 'corrupt' else content
            return ref
    api = RetainingApi()
    # Required owner paths now undergo pre-upload repair even when absent.
    # Seed the existing trusted path so this test isolates post-commit changes.
    api.files[name] = content
    target = settings.snapshots_dir / 'r1' / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    if damage == 'none':
        assert p.publish(settings, release_id='r2', api=api)['state'] == 'published'
    elif damage == 'secret':
        with pytest.raises(ValueError, match='configured secret'):
            p.publish(settings, release_id='r2', api=api)
        assert not api.commits
    else:
        with pytest.raises(p.PipelineError, match='post_upload_failed'):
            p.publish(settings, release_id='r2', api=api)
        with StateStore(settings.state_db) as store:
            assert store.get_release('r2')['state'] == 'quarantined'
    assert (name, 'b' * 40) in api.downloads
