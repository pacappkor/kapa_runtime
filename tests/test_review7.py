# SPDX-License-Identifier: GPL-3.0-only
import json

import pytest


pytestmark = pytest.mark.slow

from kapa_runtime import pipeline as p
from kapa_runtime.config import RuntimeSettings, ConfigurationError
from kapa_runtime.releases.hf import _HubWriter, PublishError
from kapa_runtime.storage.state import StateStore
from test_hf_publish import FakeSecret, manifest_for
from test_pipeline import settings, collected, SimulatedProcessExit
from test_public_promotion import MultiRepoApi, public_build


def publisher(api, **kwargs):
    return _HubWriter(api, 'owner', 'data', role='market', secret_values=lambda: [FakeSecret()], **kwargs)


@pytest.mark.parametrize('repos', [
    {'market': {'name': 'market', 'staging_name': 'KNOWLEDGE'}, 'knowledge': 'knowledge'},
    {'market': {'name': 'market', 'staging_name': 'MARKET'}},
    {'market': {'name': 'market', 'staging_name': 'OTHER-staging'}, 'knowledge': 'other'},
    {'market': 'data', 'knowledge': 'DATA'},
    {'market': 'data', 'knowledge': 'data-STAGING'},
])
def test_staging_aliases(tmp_path, repos):
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'workspace': '.', 'hf_repos': repos}))
    with pytest.raises(ConfigurationError):
        RuntimeSettings.load(path)


def test_failed_staging_replaced(settings):
    settings = public_build(settings)
    api = MultiRepoApi()
    api.failure = 'staging_verify'
    with pytest.raises(p.PipelineError):
        p.publish(settings, release_id='r1', api=api)
    repo = api.repos['owner/data-staging']
    repo['files']['data/failed-partition.parquet'] = b'old'
    repo['sha'] = 'e' * 40
    api.history[('owner/data-staging', repo['sha'])] = dict(repo['files'])
    api.failure = None
    collected(settings, 'r2')
    p.build(settings, release_id='r2')
    assert p.publish(settings, release_id='r2', api=api)['state'] == 'published'
    assert 'data/failed-partition.parquet' not in repo['files']


def test_failed_enumeration_no_baseline(tmp_path):
    api = MultiRepoApi()
    api.create_repo('owner/data', private=True)
    pub = publisher(api)
    original = api.list_repo_files
    def unavailable(*a, **k):
        raise OSError('synthetic failure')
    api.list_repo_files = unavailable
    with StateStore(tmp_path / 'state.sqlite') as store:
        assert not pub.verify('b' * 40, manifest_for({'data.json': b'{}'}), tmp_path, evidence_store=store).passed
        assert store.repository_evidence('owner/data') is None
        api.list_repo_files = original
        sha = pub._replace_files({'data.json': b'{}', '.gitattributes': b'*.json text'}, preserve_attributes=False)
        manifest = manifest_for({'data.json': b'{}'})
        manifest['hugging_face_revisions'][0]['commit_sha'] = sha
        assert pub.verify(sha, manifest, tmp_path, evidence_store=store).passed


def test_concurrent_head_change():
    api = MultiRepoApi()
    api.create_repo('owner/data', private=True)
    original_list = api.list_repo_files
    original_commit = api.create_commit
    def race(repo_id, *, revision, **kwargs):
        assert revision == 'a' * 40
        result = original_list(repo_id, revision=revision, **kwargs)
        api.repos[repo_id]['sha'] = 'f' * 40
        return result
    def commit(repo_id, *, parent_commit, **kwargs):
        assert parent_commit == 'a' * 40
        if parent_commit != api.repos[repo_id]['sha']:
            raise ValueError('concurrent change')
        return original_commit(repo_id, **kwargs)
    api.list_repo_files, api.create_commit = race, commit
    with pytest.raises(ValueError, match='concurrent'):
        publisher(api)._replace_files({'data.json': b'{}'})
    assert not api.commits


@pytest.mark.parametrize('damage', ['extra', 'hash', 'download'])
def test_restore_verification_keeps_journal(settings, damage):
    settings = public_build(settings)
    api = MultiRepoApi()
    api.failure = 'promote_crash'
    with pytest.raises(SimulatedProcessExit):
        p.publish(settings, release_id='r1', api=api)
    with StateStore(settings.state_db) as store:
        pending = store.promotion('r1')
        pending['restore_payloads'] = {'old.json': b'old'.hex()}
        pending['public_commit_sha'] = api.repos['owner/data']['sha']
        store.save_promotion('r1', pending)
    original = api.create_commit
    def corrupt(repo_id, **kwargs):
        assert kwargs['parent_commit'] == api.repos[repo_id]['sha']
        result = original(repo_id, **kwargs)
        files = api.history[(repo_id, result.oid)]
        if damage == 'extra':
            files['unexpected.json'] = b'extra'
        elif damage == 'hash':
            files['old.json'] = b'corrupt'
        else:
            api.failure = 'public_verify'
        return result
    api.create_commit = corrupt
    with pytest.raises(Exception):
        p.activate(settings, release_id='r1', api=api)
    with StateStore(settings.state_db) as store:
        assert store.promotion('r1') is not None


def test_revoked_terms_recovery(settings):
    settings = public_build(settings)
    api = MultiRepoApi()
    api.failure = 'promote_crash'
    with pytest.raises(SimulatedProcessExit):
        p.publish(settings, release_id='r1', api=api)
    # Simulate durable post-commit journal evidence (unknown-head crashes now refuse).
    with StateStore(settings.state_db) as store:
        pending = store.promotion('r1')
        pending['public_commit_sha'] = api.repos['owner/data']['sha']
        store.save_promotion('r1', pending)
    path = settings.data_root / 'operator-policy.json'
    policy = json.loads(path.read_text())
    for entry in policy['services'].values():
        entry['terms_reviewed'] = False
    path.write_text(json.dumps(policy))
    with pytest.raises(p.PipelineError, match='public_promotion_restored_quarantined'):
        p.activate(settings, release_id='r1', api=api)
    assert api.repos['owner/data']['files'] == {}
    with pytest.raises(PublishError, match='terms'):
        p._publisher(settings, api)


def test_restore_only_restrictions(tmp_path):
    api = MultiRepoApi()
    api.create_repo('owner/data', private=False)
    with pytest.raises(TypeError):
        publisher(api, expected_visibility='public', restore_payloads={'old.json': b'old'})
    with pytest.raises(PublishError, match='authority'):
        publisher(api, expected_visibility='public', restoration={'old.json': b'old'})


@pytest.mark.parametrize('name', ['api_key.txt', 'TOKEN.json', '.env', 'TESTKEY-not-real.json',
    'fake%2Bcredential%3Dvalue.json', 'fake+credential=value'.encode().hex() + '.json'])
def test_replacement_path_scan(name):
    class ExtraSecret:
        def reveal(self):
            return 'fake+credential=value'
    class NoCalls(MultiRepoApi):
        def repo_info(self, *a, **k):
            pytest.fail('Hub called before scan')
    pub = _HubWriter(NoCalls(), 'owner', 'data', role='market',
                      secret_values=lambda: [FakeSecret(), ExtraSecret()])
    with pytest.raises(PublishError):
        pub._replace_files({name: b'{}'})


@pytest.mark.parametrize('attributes_only', [True, False])
def test_first_promotion_empty_baseline(settings, attributes_only):
    settings = public_build(settings)
    api = MultiRepoApi()
    api.create_repo('owner/data', private=False)
    files = {'.gitattributes': b'*.parquet filter=lfs'}
    if not attributes_only:
        files['existing.json'] = b'preexisting'
    api.repos['owner/data']['files'] = files.copy()
    api.history[('owner/data', 'a' * 40)] = files.copy()
    if attributes_only:
        api.failure = 'promote_crash'
        with pytest.raises(SimulatedProcessExit):
            p.publish(settings, release_id='r1', api=api)
        with StateStore(settings.state_db) as store:
            assert store.promotion('r1')['restore_payloads'] == {k: v.hex() for k, v in files.items()}
    else:
        with pytest.raises(p.PipelineError, match='public_repo_not_empty_no_restore_target'):
            p.publish(settings, release_id='r1', api=api)
        assert api.repos['owner/data']['files'] == files
        assert not [c for c in api.commits if c[0] == 'owner/data']
        with StateStore(settings.state_db) as store:
            assert store.promotion('r1') is None
