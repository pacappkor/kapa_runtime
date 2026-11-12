# SPDX-License-Identifier: GPL-3.0-only
from dataclasses import replace
import hashlib
import json

import pytest
from huggingface_hub import CommitOperationAdd

from kapa_runtime import accumulation as a
from kapa_runtime.credentials import Secret
from kapa_runtime.releases.batch import publish_batch
from kapa_runtime.releases.hf import _HubWriter, PublishError
from kapa_runtime.releases.manifest import dump_manifest, manifest_digest
from kapa_runtime.releases.spool import Spool, inventory_digest
from kapa_runtime.storage.state import StateStore
from test_accumulation import setup, client, NOW
from test_public_promotion import MultiRepoApi


@pytest.fixture
def publication(setup, monkeypatch):
    settings = replace(setup, hf_namespace='owner', hf_repos={'market': 'data'},
                       hf_visibilities={'market': 'public'})
    api = MultiRepoApi()
    for repo, private in [('owner/data', False), ('owner/data-staging', True)]:
        api.create_repo(repo, private=private)
    parent = a.p._read_manifest(settings.active_manifest)
    payloads = {f['path']: (settings.snapshots_dir / 'A' / f['path']).read_bytes()
                for part in parent['selected_partitions'] for f in part['files']}
    payloads['README.md'] = b'fixture bootstrap'
    parent['files'] = [dict(path='README.md', sha256=hashlib.sha256(payloads['README.md']).hexdigest(), rows=0)]
    (settings.snapshots_dir / 'A' / 'README.md').write_bytes(payloads['README.md'])
    for repo in api.repos:
        api.create_commit(repo, operations=[CommitOperationAdd(path_in_repo=n, path_or_fileobj=b)
                                           for n, b in payloads.items()])
    parent['state'] = 'published'
    parent['github_revisions'] = {'pacappkor/kapa_runtime': 'a' * 40}
    parent['hugging_face_revisions'] = [dict(repo_id='owner/data', commit_sha=api.repos['owner/data']['sha'])]
    parent['staging_revision'] = dict(repo_id='owner/data-staging', commit_sha=api.repos['owner/data-staging']['sha'])
    dump_manifest(parent, settings.active_manifest)
    dump_manifest(parent, settings.manifests_dir / 'A.json')
    with StateStore(settings.state_db) as store:
        row = store.get_release('A')
        row['guard_evidence'][0].update(at='2020-01-01T00:00:00+00:00')
        row['guard_evidence'][0]['evidence']['published_manifest_sha256'] = manifest_digest(parent)
        store._connection.execute('UPDATE releases SET guard_evidence=? WHERE release_id=?',
                                  (json.dumps(row['guard_evidence']), 'A'))
    def writer(settings, api=None, *, staging=False, **kwargs):
        return _HubWriter(api, 'owner', 'data-staging' if staging else 'data', role='market',
            expected_visibility='private' if staging else 'public',
            terms_evidence=a._terms(a.p._policy(settings)), restoration=kwargs.get('restoration'),
            secret_values=lambda: [Secret('TESTKEY-not-real')])
    monkeypatch.setattr(a.p, '_publisher', writer)
    with client() as http:
        result = a.collect_batch(settings, job_id='B', client=http, clock=lambda: NOW)
    assert result['state'] == 'validated', result
    return settings, api


def test_frozen_batch_publication(publication):
    settings, api = publication
    result = publish_batch(settings, job_id='B', dry_run=True, api=api)
    assert result['staging']['ops'] == 6
    assert result['public']['ops'] == 6
    assert len(api.commits) == 2
    result = publish_batch(settings, job_id='B', api=api)
    assert result['state'] == 'published'
    assert len(api.commits) == 4
    with StateStore(settings.state_db) as store:
        assert store.get_release('B')['state'] == 'published'
        assert store._connection.execute("SELECT state FROM collection_jobs WHERE job_id='B'").fetchone()[0] == 'published'
    assert publish_batch(settings, job_id='B', api=api) == result
    assert len(api.commits) == 4


def test_spool_corruption(tmp_path):
    spool = Spool(tmp_path / 'spool')
    entries = spool.freeze({'README.md': b'hello'})
    api = MultiRepoApi()
    writer = _HubWriter(api, 'owner', 'data', role='market', secret_values=lambda: [Secret('TESTKEY-not-real')])
    assert spool.replay(entries, inventory_digest(entries), writer) == {'README.md': b'hello'}
    (spool.root / entries[0]['sha256']).write_bytes(b'other')
    with pytest.raises(PublishError, match='digest'):
        spool.replay(entries, inventory_digest(entries), writer)


def test_delta_refuses_rogue_and_tombstone(tmp_path):
    api = MultiRepoApi()
    api.create_repo('owner/data', private=True)
    api.create_commit('owner/data', operations=[CommitOperationAdd(path_in_repo='rogue', path_or_fileobj=b'x')])
    writer = _HubWriter(api, 'owner', 'data', role='market', secret_values=lambda: [Secret('TESTKEY-not-real')])
    head = api.repos['owner/data']['sha']
    with pytest.raises(PublishError, match='tombstone required'):
        writer._delta_plan({}, head, tmp_path)
    with pytest.raises(PublishError, match='resurrection'):
        writer._delta_plan({'rogue': b'x'}, head, tmp_path, ['rogue'])
    ops, count = writer._delta_plan({'rogue': b'x'}, head, tmp_path)
    assert not ops and count == dict(bytes=0, ops=0, files=1)
    ops, count = writer._delta_plan({}, head, tmp_path, ['rogue'])
    assert len(ops) == 1


@pytest.mark.parametrize('crash', [False, True])
def test_lost_response(publication, crash):
    settings, api = publication
    original = api.create_commit
    fired = False
    def lost(repo, **kwargs):
        nonlocal fired
        result = original(repo, **kwargs)
        if repo == 'owner/data' and not fired:
            fired = True
            if crash:
                raise KeyboardInterrupt()
            raise OSError('lost response')
        return result
    api.create_commit = lost
    if crash:
        with pytest.raises(KeyboardInterrupt):
            publish_batch(settings, job_id='B', api=api)
    result = publish_batch(settings, job_id='B', api=api)
    assert result['state'] == 'published'
    assert len(api.commits) == 4


def test_rate_limit_stops_and_resumes(publication):
    import httpx
    settings, api = publication
    original = api.create_commit
    calls, waits = [], []
    def limited(*args, **kwargs):
        calls.append(1)
        response = httpx.Response(429, headers={'Retry-After': '2'}, request=httpx.Request('POST', 'https://example.invalid'))
        raise httpx.HTTPStatusError('rate', request=response.request, response=response)
    api.create_commit = limited
    with pytest.raises(PublishError, match='exhausted'):
        publish_batch(settings, job_id='B', api=api, sleep=waits.append)
    assert len(calls) == 3 and waits == [2, 2]
    api.create_commit = original
    assert publish_batch(settings, job_id='B', api=api)['state'] == 'published'


def test_journal_corruption_blocks_replay(publication):
    settings, api = publication
    original = api.create_commit
    api.create_commit = lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        publish_batch(settings, job_id='B', api=api)
    with StateStore(settings.state_db) as store:
        store._connection.execute("UPDATE batch_publications SET data='{}'")
    api.create_commit = original
    with pytest.raises(PublishError, match='corrupt'):
        publish_batch(settings, job_id='B', api=api)
    assert len(api.commits) == 2


def test_parent_changed_before_commit(publication, monkeypatch):
    settings, api = publication
    from kapa_runtime.releases import batch
    original = batch.check_parent
    calls = []
    def check(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise ValueError('Parent changed')
        return original(*args, **kwargs)
    monkeypatch.setattr(batch, 'check_parent', check)
    with pytest.raises(ValueError, match='Parent changed'):
        publish_batch(settings, job_id='B', api=api)
    assert len(api.commits) == 2


def test_operation_cap(tmp_path):
    api = MultiRepoApi()
    api.create_repo('owner/data', private=True)
    writer = _HubWriter(api, 'owner', 'data', role='market', secret_values=lambda: [Secret('TESTKEY-not-real')])
    with pytest.raises(PublishError, match='cap'):
        writer._delta_plan({f'files/{i}.txt': b'x' for i in range(501)}, 'a' * 40, tmp_path)


def test_publish_activate_reobserve_rollback(publication, monkeypatch):
    settings, api = publication
    monkeypatch.setattr(a.p, '_verify_workspace', lambda *args: None)
    publish_batch(settings, job_id='B', api=api)
    with StateStore(settings.state_db) as store:
        m = a.p._trusted_published_manifest(settings, store, 'B')
        w = a.p._publisher(settings, api)
        report = w.verify(m['hugging_face_revisions'][0]['commit_sha'], m, settings.cache_dir / 'debug', previous_files=a.p._previous_repo_files(settings, store, w.repo_id, as_of='B'), evidence_store=store)
        assert report.passed, report.errors
    a.p.activate(settings, release_id='B', api=api, cumulative=True)
    before = a.p._read_manifest(settings.active_manifest)
    with client() as http:
        assert a.collect_batch(settings, job_id='C', client=http, clock=lambda: NOW)['state'] == 'validated'
    publish_batch(settings, job_id='C', api=api)
    a.p.activate(settings, release_id='C', api=api, cumulative=True)
    after = a.p._read_manifest(settings.active_manifest)
    assert before['coverage'] == after['coverage']
    a.p.rollback(settings, api=api, cumulative=True)
    assert a.p._read_manifest(settings.active_manifest)['release_id'] == 'B'


@pytest.mark.parametrize('restore_fails', [False, True])
def test_public_verification_failure_restores_or_blocks(publication, monkeypatch, restore_fails):
    settings, api = publication
    original = _HubWriter.verify
    def failed(self, sha, manifest, *args, **kwargs):
        report = original(self, sha, manifest, *args, **kwargs)
        if self.repo_id == 'owner/data' and manifest['release_id'] == 'B':
            return replace(report, errors=('synthetic_failure',))
        return report
    monkeypatch.setattr(_HubWriter, 'verify', failed)
    commit = api.create_commit
    def restoring(repo, **kwargs):
        if restore_fails and repo == 'owner/data' and len(api.commits) >= 4:
            raise OSError('restore failed')
        return commit(repo, **kwargs)
    api.create_commit = restoring
    before = dict(api.repos['owner/data']['files'])
    with pytest.raises((a.p.PipelineError, OSError)):
        publish_batch(settings, job_id='B', api=api)
    with StateStore(settings.state_db) as store:
        assert bool(store.pending_promotions()) == restore_fails
        assert bool(store._connection.execute('SELECT 1 FROM batch_publications').fetchone()) == restore_fails
    if not restore_fails:
        assert api.repos['owner/data']['files'] == before


def test_private_visibility_refusal(publication):
    settings, api = publication
    api.repos['owner/data-staging']['private'] = False
    with pytest.raises(PublishError, match='visibility'):
        publish_batch(settings, job_id='B', api=api)
    assert len(api.commits) == 2


def test_commit_operation_counts_and_full_verification(publication):
    settings, api = publication
    original = api.create_commit
    counts = []
    def commit(repo, *, operations, **kwargs):
        counts.append((repo, len(operations)))
        return original(repo, operations=operations, **kwargs)
    api.create_commit = commit
    result = publish_batch(settings, job_id='B', api=api)
    assert counts == [('owner/data-staging', 6), ('owner/data', 6)]
    sha = result['commit_sha']
    downloaded = {name for repo, rev, name in api.downloads if repo == 'owner/data' and rev == sha}
    assert downloaded == set(api.repos['owner/data']['files'])


@pytest.mark.parametrize('name,payload', [('TESTKEY-not-real.txt', b'ok'), ('file.txt', b'TESTKEY-not-real')])
def test_delta_secret_refusal(tmp_path, name, payload):
    api = MultiRepoApi()
    api.create_repo('owner/data', private=True)
    writer = _HubWriter(api, 'owner', 'data', role='market', secret_values=lambda: [Secret('TESTKEY-not-real')])
    with pytest.raises(PublishError):
        writer._delta_plan({name: payload}, 'a' * 40, tmp_path)
    assert not api.commits


def test_delta_byte_cap(tmp_path):
    class AdvertisedBytes(bytes):
        def __len__(self):
            return 512 * 1024 * 1024 + 1
    api = MultiRepoApi()
    api.create_repo('owner/data', private=True)
    writer = _HubWriter(api, 'owner', 'data', role='market', secret_values=lambda: [Secret('TESTKEY-not-real')])
    with pytest.raises(PublishError, match='cap'):
        writer._delta_plan({'large.txt': AdvertisedBytes(b'x')}, 'a' * 40, tmp_path)


def test_remote_head_race(publication):
    settings, api = publication
    original = api.create_commit
    def race(repo, *, parent_commit, **kwargs):
        assert parent_commit == api.repos[repo]['sha']
        api.repos[repo]['sha'] = 'f' * 40
        api.history[(repo, 'f' * 40)] = {'rogue.txt': b'other writer'}
        raise OSError('parent changed')
    api.create_commit = race
    with pytest.raises(PublishError):
        publish_batch(settings, job_id='B', api=api)
    assert len(api.commits) == 2
