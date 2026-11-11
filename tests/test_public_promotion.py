# SPDX-License-Identifier: GPL-3.0-only
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest


pytestmark = pytest.mark.slow
from huggingface_hub import CommitOperationDelete
from huggingface_hub.errors import RepositoryNotFoundError

from kapa_runtime import pipeline as p
from kapa_runtime.config import RuntimeSettings
from kapa_runtime.releases.hf import _HubWriter
from kapa_runtime.releases.manifest import dump_manifest, load_manifest
from kapa_runtime.storage.state import StateStore
from test_hf_publish import FakeSecret, manifest_for
from test_pipeline import settings, collected, SimulatedProcessExit, http


class MultiRepoApi:
    token = 'TESTKEY-not-real'

    def __init__(self):
        self.repos = {}
        self.history = {}
        self.commits = []
        self.downloads = []
        self.failure = None

    def repo_info(self, repo_id, **kwargs):
        if repo_id not in self.repos:
            raise RepositoryNotFoundError('missing', response=httpx.Response(404, request=httpx.Request('GET', 'https://example.invalid')))
        return SimpleNamespace(private=self.repos[repo_id]['private'], sha=self.repos[repo_id]['sha'])

    def create_repo(self, repo_id, *, private, **kwargs):
        self.repos[repo_id] = {'private': private, 'files': {}, 'sha': 'a' * 40}
        self.history[(repo_id, 'a' * 40)] = {}

    def create_commit(self, repo_id, *, operations, **kwargs):
        repo = self.repos[repo_id]
        files = dict(repo['files'])
        for op in operations:
            if isinstance(op, CommitOperationDelete):
                files.pop(op.path_in_repo, None)
            else:
                files[op.path_in_repo] = op.path_or_fileobj
        sha = format(len(self.commits) + 1, '040x')
        repo.update(files=files, sha=sha)
        self.history[(repo_id, sha)] = dict(files)
        self.commits.append((repo_id, sha, dict(files)))
        if self.failure == 'promote_crash' and repo_id == 'owner/data':
            self.failure = None
            raise SimulatedProcessExit()
        return SimpleNamespace(oid=sha)

    def list_repo_files(self, repo_id, *, revision=None, **kwargs):
        files = self.repos[repo_id]['files'] if revision is None else self.history[(repo_id, revision)]
        return list(files)

    def get_paths_info(self, repo_id, paths, *, revision, expand, **kwargs):
        assert expand is True
        files = self.history[(repo_id, revision)]
        return [SimpleNamespace(path=name, size=len(files[name]), blob_id=hashlib.sha1(files[name]).hexdigest()) for name in paths]

    def hf_hub_download(self, repo_id, filename, *, revision, cache_dir, **kwargs):
        self.downloads.append((repo_id, revision, filename))
        if self.failure == 'staging_verify' and repo_id.endswith('-staging'):
            raise OSError('synthetic private verify failure')
        if self.failure == 'public_verify' and repo_id == 'owner/data':
            raise OSError('synthetic public verify failure')
        path = Path(cache_dir) / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.history[(repo_id, revision)][filename])
        return str(path)


def public_build(settings, release='r1'):
    settings = replace(settings, hf_visibilities={'market': 'public'})
    collected(settings, release)
    p.build(settings, release_id=release)
    return settings


@pytest.mark.parametrize('failure', ['staging_verify', 'public_verify', 'none'])
def test_private_verification_precedes_public_promotion(settings, failure):
    settings = public_build(settings)
    api = MultiRepoApi()
    api.failure = failure
    if failure == 'none':
        result = p.publish(settings, release_id='r1', api=api)
        assert result['state'] == 'published'
        manifest = load_manifest(p._manifest_path(settings, 'r1'))
        assert manifest['staging_revision']['repo_id'] == 'owner/data-staging'
        assert manifest['hugging_face_revisions'][0]['repo_id'] == 'owner/data'
        assert api.commits[0][2] == api.commits[1][2]
        p.activate(settings, release_id='r1', api=api)
    else:
        with pytest.raises(p.PipelineError):
            p.publish(settings, release_id='r1', api=api)
        public = [c for c in api.commits if c[0] == 'owner/data']
        assert len(public) == (0 if failure == 'staging_verify' else 2)
        if public:
            assert public[-1][2] == {}
        with StateStore(settings.state_db) as store:
            assert store.get_release('r1')['state'] == 'quarantined'
    assert api.repos['owner/data-staging']['private'] is True


@pytest.mark.parametrize('retry', ['publish', 'activate'])
def test_interrupted_unknown_promotion_fails_closed(settings, retry):
    settings = public_build(settings)
    api = MultiRepoApi()
    api.failure = 'promote_crash'
    with pytest.raises(SimulatedProcessExit):
        p.publish(settings, release_id='r1', api=api)
    inventory = dict(api.repos['owner/data']['files'])
    with pytest.raises(p.PipelineError, match='public_recovery_expected_head_required'):
        getattr(p, retry)(settings, release_id='r1', api=api)
    assert api.repos['owner/data']['files'] == inventory
    with StateStore(settings.state_db) as store:
        assert store.promotion('r1') is not None


def test_public_promotion_keeps_active_partition_and_complete_staging_inventory(settings):
    from huggingface_hub import CommitOperationAdd
    settings = public_build(settings)
    api = MultiRepoApi()
    attributes = b'*.parquet filter=lfs diff=lfs merge=lfs -text\n'
    for repo, private in [('owner/data', False), ('owner/data-staging', True)]:
        api.create_repo(repo, private=private)
        api.create_commit(repo, operations=[CommitOperationAdd(path_in_repo='.gitattributes', path_or_fileobj=attributes)])
    p.publish(settings, release_id='r1', api=api)
    p.activate(settings, release_id='r1', api=api)
    active_pointer = settings.active_manifest.read_bytes()
    old_manifest = load_manifest(p._manifest_path(settings, 'r1'))
    old_partitions = {f['path']: api.repos['owner/data']['files'][f['path']]
                      for f in old_manifest['files'] if f['path'].endswith('.parquet')}
    with http() as client:
        for service in p.SERVICES:
            p.collect(settings, service=service, lawd_code='22220', deal_ymd='202510',
                      release_id='r2', client=client)
    p.build(settings, release_id='r2')
    p.publish(settings, release_id='r2', api=api)
    manifest = load_manifest(p._manifest_path(settings, 'r2'))
    staging_sha = manifest['staging_revision']['commit_sha']
    public_sha = manifest['hugging_face_revisions'][0]['commit_sha']
    staging_files = api.history[('owner/data-staging', staging_sha)]
    public_files = api.history[('owner/data', public_sha)]
    assert public_files == staging_files
    assert public_files['.gitattributes'] == attributes
    assert all(public_files[name] == payload for name, payload in old_partitions.items())
    assert old_partitions.keys().isdisjoint({f['path'] for f in manifest['files']})
    assert settings.active_manifest.read_bytes() == active_pointer


@pytest.mark.parametrize('window', ['verified', 'snapshot_complete'])
def test_private_publication_requires_current_verified_head(settings, monkeypatch, window):
    collected(settings)
    p.build(settings, release_id='r1')
    api = MultiRepoApi()
    original_verify = _HubWriter.verify
    original_dump = p.dump_manifest
    def advance():
        from huggingface_hub import CommitOperationAdd
        api.create_commit('owner/data', operations=[CommitOperationAdd(path_in_repo='foreign.txt', path_or_fileobj=b'foreign')])
    def verify(self, *args, **kwargs):
        report = original_verify(self, *args, **kwargs)
        if window == 'verified':
            advance()
        return report
    def dump(manifest, path):
        original_dump(manifest, path)
        if window == 'snapshot_complete' and manifest['state'] == 'published':
            advance()
    monkeypatch.setattr(_HubWriter, 'verify', verify)
    monkeypatch.setattr(p, 'dump_manifest', dump)
    with pytest.raises(p.PipelineError, match='post_upload_failed'):
        p.publish(settings, release_id='r1', api=api)
    with StateStore(settings.state_db) as store:
        row = store.get_release('r1')
        assert row['state'] == 'quarantined'
        assert not any(e['state'] == 'published' for e in row['guard_evidence'])


def test_failed_public_candidate_restores_previous_active_inventory(settings):
    settings = public_build(settings)
    api = MultiRepoApi()
    p.publish(settings, release_id='r1', api=api)
    p.activate(settings, release_id='r1', api=api)
    inventory = dict(api.repos['owner/data']['files'])
    active = settings.active_manifest.read_bytes()
    collected(settings, 'r2')
    p.build(settings, release_id='r2')
    # Fail only the new public commit, not the old revision verification.
    original = api.hf_hub_download
    def download(repo, name, *, revision, **kwargs):
        if repo == 'owner/data' and revision == format(4, '040x'):
            raise OSError('synthetic candidate failure')
        return original(repo, name, revision=revision, **kwargs)
    api.hf_hub_download = download
    with pytest.raises(p.PipelineError, match='public_promotion_restored_quarantined'):
        p.publish(settings, release_id='r2', api=api)
    assert api.repos['owner/data']['files'] == inventory
    assert settings.active_manifest.read_bytes() == active


@pytest.mark.parametrize('fail', [False, True])
def test_publication_before_first_activation_preserves_trusted_inventory(settings, monkeypatch, fail):
    settings = public_build(settings)
    api = MultiRepoApi()
    p.publish(settings, release_id='r1', api=api)
    before = dict(api.repos['owner/data']['files'])
    assert not settings.active_manifest.exists()
    with http() as client:
        for service in p.SERVICES:
            p.collect(settings, service=service, lawd_code='22220', deal_ymd='202510', release_id='r2', client=client)
    p.build(settings, release_id='r2')
    if fail:
        verify = _HubWriter.verify
        def reject(self, sha, manifest, *args, **kwargs):
            if self.repo_id == 'owner/data' and manifest['release_id'] == 'r2':
                raise ValueError('synthetic candidate failure')
            return verify(self, sha, manifest, *args, **kwargs)
        monkeypatch.setattr(_HubWriter, 'verify', reject)
        with pytest.raises(p.PipelineError, match='public_promotion_restored_quarantined'):
            p.publish(settings, release_id='r2', api=api)
        assert api.repos['owner/data']['files'] == before
    else:
        assert p.publish(settings, release_id='r2', api=api)['state'] == 'published'
        assert all(api.repos['owner/data']['files'][name] == value for name, value in before.items()
                   if name.endswith('.parquet'))


@pytest.mark.parametrize('damage', ['missing', 'corrupt'])
@pytest.mark.parametrize('repairable', [True, False])
def test_active_staging_partition_repaired_from_verified_public(settings, repairable, damage):
    settings = public_build(settings)
    api = MultiRepoApi()
    p.publish(settings, release_id='r1', api=api)
    p.activate(settings, release_id='r1', api=api)
    before = dict(api.repos['owner/data']['files'])
    missing = next(name for name in before if name.endswith('.parquet'))
    from huggingface_hub import CommitOperationDelete
    from huggingface_hub import CommitOperationAdd
    operation = (CommitOperationDelete(path_in_repo=missing) if damage == 'missing' else
                 CommitOperationAdd(path_in_repo=missing, path_or_fileobj=b'corrupt'))
    api.create_commit('owner/data-staging', operations=[operation])
    if not repairable:
        (settings.snapshots_dir / 'r1' / missing).unlink()
    with http() as client:
        for service in p.SERVICES:
            p.collect(settings, service=service, lawd_code='22220', deal_ymd='202510', release_id='r2', client=client)
    p.build(settings, release_id='r2')
    p.publish(settings, release_id='r2', api=api)
    assert api.repos['owner/data-staging']['files'][missing] == before[missing]
    assert api.repos['owner/data']['files'][missing] == before[missing]


@pytest.mark.parametrize('operation', ['reactivate', 'rollback'])
def test_historical_revision_excludes_later_different_scope_owners(settings, operation):
    api = MultiRepoApi()
    for release, region in [('r1', '11110'), ('r2', '22220')]:
        with http() as client:
            for service in p.SERVICES:
                p.collect(settings, service=service, lawd_code=region, deal_ymd='202510', release_id=release, client=client)
        p.build(settings, release_id=release)
        p.publish(settings, release_id=release, api=api)
        p.activate(settings, release_id=release, api=api)
    first = load_manifest(p._manifest_path(settings, 'r1'))
    with StateStore(settings.state_db) as store:
        historic = p._previous_repo_files(settings, store, 'owner/data', as_of='r1')
        current = p._previous_repo_files(settings, store, 'owner/data')
    assert not any('lawd_code=22220/' in entry['path'] for entry in historic)
    assert any('lawd_code=22220/' in entry['path'] for entry in current)
    if operation == 'reactivate':
        p.activate(settings, release_id='r1', api=api)
    else:
        assert p.rollback(settings, api=api)['release_id'] == 'r1'
    assert load_manifest(settings.active_manifest) == first


@pytest.mark.parametrize('operation', ['activate', 'rollback'])
def test_historical_target_honors_removal_of_later_restored_active_owner(settings, operation):
    from huggingface_hub import CommitOperationAdd

    collected(settings, 'a')
    p.build(settings, release_id='a')
    api = MultiRepoApi()
    p.publish(settings, release_id='a', api=api)
    missing = next(name for name in api.repos['owner/data']['files'] if name.endswith('.parquet'))
    payload = api.repos['owner/data']['files'][missing]
    # A was removed before C's publication, so C's pinned revision should
    # not require this path even if A becomes the active owner later.
    api.create_commit('owner/data', operations=[CommitOperationDelete(path_in_repo=missing)])
    with StateStore(settings.state_db) as store:
        store._record_serving_removal('a', missing, 'owner/data')
    with http() as client:
        for service in p.SERVICES:
            p.collect(settings, service=service, lawd_code='22220', deal_ymd='202510',
                      release_id='c', client=client)
    p.build(settings, release_id='c')
    p.publish(settings, release_id='c', api=api)
    target = load_manifest(p._manifest_path(settings, 'c'))
    for ref in target['hugging_face_revisions']:
        assert missing not in api.history[(ref['repo_id'], ref['commit_sha'])]
    p.activate(settings, release_id='c', api=api)

    api.create_commit('owner/data', operations=[
        CommitOperationAdd(path_in_repo=missing, path_or_fileobj=payload)])
    p.activate(settings, release_id='a', api=api)
    assert load_manifest(settings.active_manifest)['release_id'] == 'a'
    with StateStore(settings.state_db) as store:
        current = p._previous_repo_files(settings, store, 'owner/data')
        assert any(entry['path'] == missing for entry in current)
    if operation == 'activate':
        result = p.activate(settings, release_id='c', api=api)
    else:
        result = p.rollback(settings, api=api)
    assert result == {'release_id': 'c', 'state': 'active'}
    assert load_manifest(settings.active_manifest) == target


@pytest.mark.parametrize('operation', ['activate', 'rollback'])
def test_historical_target_overwritten_at_head_is_refused(settings, operation):
    import pyarrow as pa
    import pyarrow.parquet as pq
    api = MultiRepoApi()
    collected(settings, 'r1')
    for release in ('r1', 'r2', 'r3'):
        if release != 'r1':
            collected(settings, release)
        if release == 'r3':
            meta_path = p._metadata(settings, release)
            meta = json.loads(meta_path.read_text(encoding='utf-8'))
            for completed in meta['completed'].values():
                for entry in completed['files']:
                    path = settings.staging_dir / release / entry['path']
                    table = pq.read_table(pa.BufferReader(path.read_bytes()))
                    pq.write_table(table, path, compression='NONE')
                    entry['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
            p._json_write(meta_path, meta)
        p.build(settings, release_id=release)
        p.publish(settings, release_id=release, api=api)
        p.activate(settings, release_id=release, api=api)
    first = load_manifest(p._manifest_path(settings, 'r1'))
    before = settings.active_manifest.read_bytes()
    if operation == 'rollback':
        dump_manifest(first, settings.active_manifest.with_name('previous.json'))
    with pytest.raises(p.PipelineError, match='active_target_not_served_at_head'):
        if operation == 'activate':
            p.activate(settings, release_id='r1', api=api)
        else:
            p.rollback(settings, api=api)
    assert settings.active_manifest.read_bytes() == before


def test_historically_active_release_can_be_selected_again(settings):
    api = MultiRepoApi()
    for release, region in [('r1', '11110'), ('r2', '22220'), ('r3', '33330')]:
        with http() as client:
            for service in p.SERVICES:
                p.collect(settings, service=service, lawd_code=region, deal_ymd='202510', release_id=release, client=client)
        p.build(settings, release_id=release)
        p.publish(settings, release_id=release, api=api)
        p.activate(settings, release_id=release, api=api)
    assert load_manifest(settings.active_manifest)['release_id'] == 'r3'
    assert p.activate(settings, release_id='r1', api=api)['state'] == 'active'
    assert load_manifest(settings.active_manifest)['release_id'] == 'r1'
    assert load_manifest(settings.active_manifest.with_name('previous.json'))['release_id'] == 'r3'
    with StateStore(settings.state_db) as store:
        assert sum(e['state'] == 'active' for e in store.get_release('r1')['guard_evidence']) == 1


def test_failed_promotion_restores_active_and_unactivated_published_inventory(settings, monkeypatch):
    settings = public_build(settings)
    api = MultiRepoApi()
    p.publish(settings, release_id='r1', api=api)
    p.activate(settings, release_id='r1', api=api)
    active = settings.active_manifest.read_bytes()
    for release, region in [('r2', '22220'), ('r3', '33330')]:
        with http() as client:
            for service in p.SERVICES:
                p.collect(settings, service=service, lawd_code=region, deal_ymd='202510', release_id=release, client=client)
        p.build(settings, release_id=release)
        if release == 'r2':
            p.publish(settings, release_id=release, api=api)
    before = dict(api.repos['owner/data']['files'])
    verify = _HubWriter.verify
    def fail_candidate(self, sha, manifest, *args, **kwargs):
        if self.repo_id == 'owner/data' and manifest['release_id'] == 'r3':
            raise ValueError('synthetic verification failure')
        return verify(self, sha, manifest, *args, **kwargs)
    monkeypatch.setattr(_HubWriter, 'verify', fail_candidate)
    with pytest.raises(p.PipelineError, match='public_promotion_restored_quarantined'):
        p.publish(settings, release_id='r3', api=api)
    assert api.repos['owner/data']['files'] == before
    assert settings.active_manifest.read_bytes() == active
    assert any('lawd_code=11110/' in name for name in before)
    assert any('lawd_code=22220/' in name for name in before)
    with StateStore(settings.state_db) as store:
        assert store.get_release('r2')['state'] == 'published'
        assert store.get_release('r3')['state'] == 'quarantined'


@pytest.mark.parametrize('change', ['manifest', 'files_and_manifest', 'candidate'])
def test_publish_requires_ledger_bound_validated_manifest(settings, change):
    collected(settings)
    p.build(settings, release_id='r1')
    path = p._manifest_path(settings, 'r1')
    manifest = load_manifest(path)
    if change == 'files_and_manifest':
        card = settings.staging_dir / 'r1' / 'README.md'
        card.write_text('modified synthetic card')
        next(f for f in manifest['files'] if f['path'] == 'README.md')['sha256'] = hashlib.sha256(card.read_bytes()).hexdigest()
    else:
        manifest['coverage']['complete'] = False
    dump_manifest(manifest, path if change != 'candidate' else settings.staging_dir / 'r1' / 'manifests/release.json')
    if change == 'files_and_manifest':
        dump_manifest(manifest, settings.staging_dir / 'r1' / 'manifests/release.json')
    with pytest.raises(p.PipelineError, match='validated_manifest_changed_rebuild_required'):
        p.publish(settings, release_id='r1', api=MultiRepoApi())


@pytest.mark.parametrize('change', ['bytes', 'secret', 'removed', 'none'])
def test_gitattributes_bound_per_repo_at_exact_revision(tmp_path, change):
    api = MultiRepoApi()
    api.create_repo('owner/data', private=True)
    payloads = {'data.json': b'{}', '.gitattributes': b'*.parquet filter=lfs\n'}
    from huggingface_hub import CommitOperationAdd
    def commit(values):
        return api.create_commit('owner/data', operations=[CommitOperationAdd(path_in_repo=k, path_or_fileobj=v) for k, v in values.items()]).oid
    first = commit(payloads)
    manifest = manifest_for({'data.json': b'{}'})
    manifest['hugging_face_revisions'][0]['commit_sha'] = first
    publisher = _HubWriter(api, 'owner', 'data', role='market', secret_values=lambda: [FakeSecret()])
    with StateStore(tmp_path / 'state.sqlite') as store:
        assert publisher.verify(first, manifest, tmp_path / 'cache', evidence_store=store).passed
    if change == 'removed':
        api.repos['owner/data']['files'].pop('.gitattributes')
        payloads.pop('.gitattributes')
    elif change != 'none':
        payloads['.gitattributes'] = b'TESTKEY-not-real' if change == 'secret' else b'changed'
    second = commit(payloads)
    manifest['hugging_face_revisions'][0]['commit_sha'] = second
    with StateStore(tmp_path / 'state.sqlite') as store:
        report = publisher.verify(second, manifest, tmp_path / 'cache2', evidence_store=store)
        assert report.passed is (change == 'none')
    assert ('owner/data', first, '.gitattributes') in api.downloads


def test_orphan_metadata_reparse_rejected_before_read(settings, monkeypatch):
    import stat
    root = settings.staging_dir / 'orphan'
    root.mkdir(parents=True)
    metadata = root / 'collection.json'
    metadata.write_text('{}')
    original_stat = Path.stat
    original_read = Path.read_text
    def fake_stat(path, *a, **k):
        if path == metadata:
            return SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
        return original_stat(path, *a, **k)
    def no_read(path, *a, **k):
        if path == metadata:
            pytest.fail('linked metadata read before safety check')
        return original_read(path, *a, **k)
    monkeypatch.setattr(Path, 'stat', fake_stat)
    monkeypatch.setattr(Path, 'read_text', no_read)
    with http() as client, pytest.raises(ValueError, match='reparse'):
        p.collect(settings, service=p.SERVICES[0], lawd_code='11110', deal_ymd='202510', release_id='orphan', client=client)


def test_removed_latest_owner_tombstones_older_owner_until_republication(settings):
    api = MultiRepoApi()
    for release in ('a', 'b'):
        collected(settings, release)
        p.build(settings, release_id=release)
        p.publish(settings, release_id=release, api=api)
    missing = next(name for name in api.repos['owner/data']['files'] if name.endswith('.parquet'))
    api.create_commit('owner/data', operations=[CommitOperationDelete(path_in_repo=missing)])
    (settings.snapshots_dir / 'b' / missing).unlink()
    for release in ('c', 'd'):
        with http() as client:
            for service in p.SERVICES:
                p.collect(settings, service=service, lawd_code='22220', deal_ymd='202510',
                          release_id=release, client=client)
        p.build(settings, release_id=release)
        assert p.publish(settings, release_id=release, api=api)['state'] == 'published'
        assert missing not in api.repos['owner/data']['files']
        with StateStore(settings.state_db) as store:
            assert missing not in p._retained_owners(settings, store, 'owner/data')[0]
            assert missing not in p._retained_owners(settings, store, 'owner/data', as_of=release)[0]
            assert p._retained_repair(settings, store, 'owner/data', [])(missing, []) is None
    collected(settings, 'e')
    p.build(settings, release_id='e')
    p.publish(settings, release_id='e', api=api)
    with StateStore(settings.state_db) as store:
        assert p._retained_owners(settings, store, 'owner/data')[0][missing][0] == 'e'


@pytest.mark.parametrize('removed_repo', ['owner/data', 'owner/data-staging'])
def test_removal_is_scoped_to_repository(settings, removed_repo):
    settings = public_build(settings)
    api = MultiRepoApi()
    p.publish(settings, release_id='r1', api=api)
    missing = next(name for name in api.repos['owner/data']['files'] if name.endswith('.parquet'))
    with StateStore(settings.state_db) as store:
        store._record_serving_removal('r1', missing, removed_repo)
        for repo in ('owner/data', 'owner/data-staging'):
            assert (missing in p._retained_owners(settings, store, repo)[0]) is (repo != removed_repo)


@pytest.mark.parametrize('legacy', [False, True])
def test_public_keeps_partition_lost_only_in_staging(settings, legacy):
    settings = public_build(settings)
    api = MultiRepoApi()
    p.publish(settings, release_id='r1', api=api)
    missing = next(name for name in api.repos['owner/data']['files'] if name.endswith('.parquet'))
    payload = api.repos['owner/data']['files'][missing]
    api.create_commit('owner/data-staging', operations=[CommitOperationDelete(path_in_repo=missing)])
    (settings.snapshots_dir / 'r1' / missing).unlink()
    if legacy:
        with StateStore(settings.state_db) as store:
            store._record_serving_removal('r1', missing, 'owner/data-staging')
    with http() as client:
        for service in p.SERVICES:
            p.collect(settings, service=service, lawd_code='22220', deal_ymd='202510', release_id='r2', client=client)
    p.build(settings, release_id='r2')
    assert p.publish(settings, release_id='r2', api=api)['state'] == 'published'
    assert api.repos['owner/data']['files'][missing] == payload
    assert api.repos['owner/data-staging']['files'][missing] == payload
    with StateStore(settings.state_db) as store:
        removals = [e['evidence'] for e in store.get_release('r1')['guard_evidence']
                    if e['evidence'].get('reason') == 'retained_partition_removed']
        assert bool(removals) is legacy
        assert all(e['repo_id'] == 'owner/data-staging' for e in removals)


@pytest.mark.parametrize('public', [False, True])
def test_legacy_removal_requires_unambiguous_ledger_repository(settings, public):
    if public:
        settings = public_build(settings)
    else:
        collected(settings)
        p.build(settings, release_id='r1')
    api = MultiRepoApi()
    p.publish(settings, release_id='r1', api=api)
    missing = next(name for name in api.repos['owner/data']['files'] if name.endswith('.parquet'))
    with StateStore(settings.state_db) as store:
        store._record_serving_removal('r1', missing, 'owner/data')
        history = store.get_release('r1')['guard_evidence']
        del history[-1]['evidence']['repo_id']
        store._connection.execute('UPDATE releases SET guard_evidence=? WHERE release_id=?',
                                  (json.dumps(history), 'r1'))
        if public:
            with pytest.raises(p.PipelineError, match='retained_removal_repo_unresolved'):
                p._retained_owners(settings, store, 'owner/data')
        else:
            assert missing not in p._retained_owners(settings, store, 'owner/data')[0]


def test_optional_private_staging_name_config(tmp_path):
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'workspace': '.', 'hf_repos': {'market': {'name': 'market', 'visibility': 'public', 'staging_name': 'candidate-private'}}}))
    assert RuntimeSettings.load(config).hf_staging_names['market'] == 'candidate-private'


@pytest.mark.parametrize('window', ['uploaded_transition', 'verify'])
def test_crash_after_public_commit_recovers(settings, monkeypatch, window):
    settings = public_build(settings)
    api = MultiRepoApi()
    original_transition = StateStore.transition_release
    original_verify = _HubWriter.verify
    def transition(self, release, state, evidence):
        if state == 'uploaded-candidate' and window == 'uploaded_transition':
            raise SimulatedProcessExit()
        return original_transition(self, release, state, evidence)
    def verify(self, *a, **k):
        if self.repo_id == 'owner/data' and window == 'verify':
            raise SimulatedProcessExit()
        return original_verify(self, *a, **k)
    monkeypatch.setattr(StateStore, 'transition_release', transition)
    monkeypatch.setattr(_HubWriter, 'verify', verify)
    with pytest.raises(SimulatedProcessExit):
        p.publish(settings, release_id='r1', api=api)
    monkeypatch.setattr(StateStore, 'transition_release', original_transition)
    monkeypatch.setattr(_HubWriter, 'verify', original_verify)
    with pytest.raises(p.PipelineError, match='public_promotion_restored_quarantined'):
        p.activate(settings, release_id='r1', api=api)
    assert api.repos['owner/data']['files'] == {}


def test_staging_visibility_mismatch_does_not_promote(settings):
    settings = public_build(settings)
    api = MultiRepoApi()
    api.create_repo('owner/data-staging', private=False)
    from kapa_runtime.releases.hf import PublishError
    with pytest.raises(PublishError):
        p.publish(settings, release_id='r1', api=api)
    assert not api.commits


def test_secret_gitattributes_does_not_establish_baseline(tmp_path):
    from huggingface_hub import CommitOperationAdd
    api = MultiRepoApi()
    api.create_repo('owner/data', private=True)
    sha = api.create_commit('owner/data', operations=[CommitOperationAdd(path_in_repo='data.json', path_or_fileobj=b'{}'),
        CommitOperationAdd(path_in_repo='.gitattributes', path_or_fileobj=b'TESTKEY-not-real')]).oid
    manifest = manifest_for({'data.json': b'{}'})
    manifest['hugging_face_revisions'][0]['commit_sha'] = sha
    publisher = _HubWriter(api, 'owner', 'data', role='market', secret_values=lambda: [FakeSecret()])
    with StateStore(tmp_path / 'state.sqlite') as store:
        assert not publisher.verify(sha, manifest, tmp_path / 'cache', evidence_store=store).passed
        assert store.repository_evidence('owner/data') is None


def test_orphan_metadata_symlink_rejected_before_read(settings, monkeypatch):
    root = settings.staging_dir / 'orphan'
    root.mkdir(parents=True)
    metadata = root / 'collection.json'
    metadata.write_text('{}')
    original_link = Path.is_symlink
    original_read = Path.read_text
    def linked(path):
        return path == metadata or original_link(path)
    def no_read(path, *a, **k):
        if path == metadata:
            pytest.fail('symlink target read')
        return original_read(path, *a, **k)
    monkeypatch.setattr(Path, 'is_symlink', linked)
    monkeypatch.setattr(Path, 'read_text', no_read)
    with http() as client, pytest.raises(ValueError, match='linked'):
        p.collect(settings, service=p.SERVICES[0], lawd_code='11110', deal_ymd='202510', release_id='orphan', client=client)


def test_other_release_cannot_bypass_pending_public_recovery(settings):
    settings = public_build(settings)
    api = MultiRepoApi()
    api.failure = 'promote_crash'
    with pytest.raises(SimulatedProcessExit):
        p.publish(settings, release_id='r1', api=api)
    with pytest.raises(p.PipelineError, match='public_recovery_expected_head_required'):
        p.publish(settings, release_id='another', api=api)
    assert api.repos['owner/data']['files']
    with StateStore(settings.state_db) as store:
        assert store.promotion('r1') is not None


def test_state_v1_migrates_preserving_release_evidence(tmp_path):
    import sqlite3
    path = tmp_path / 'state.sqlite'
    with StateStore(path) as store:
        store.transition_release('old', 'draft', {'services': []})
    con = sqlite3.connect(path)
    con.executescript('DROP TABLE repository_evidence; DROP TABLE public_promotions; PRAGMA user_version=1;')
    con.close()
    with StateStore(path) as store:
        assert store.get_release('old')['state'] == 'draft'
        assert store.pending_promotions() == []
        store.bind_repository_attributes('owner/data', 'a' * 64)
    with StateStore(path) as store:
        assert store.repository_evidence('owner/data')['gitattributes_sha256'] == 'a' * 64
