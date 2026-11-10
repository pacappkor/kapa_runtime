# SPDX-License-Identifier: GPL-3.0-only
import json
import hashlib
from pathlib import Path

import pytest

from kapa_runtime import pipeline as p
from kapa_runtime.releases.manifest import load_manifest, dump_manifest
from kapa_runtime.storage.state import StateStore
from test_pipeline import settings, collected
from test_public_promotion import MultiRepoApi


def legacy(settings):
    collected(settings)
    p.build(settings, release_id='r1')
    api = MultiRepoApi()
    p.publish(settings, release_id='r1', api=api)
    with StateStore(settings.state_db) as store:
        row = store.get_release('r1')
        for event in row['guard_evidence']:
            event['evidence'].pop('published_manifest_sha256', None)
        store._connection.execute('UPDATE releases SET guard_evidence=? WHERE release_id=?',
                                  (json.dumps(row['guard_evidence']), 'r1'))
    return api


@pytest.mark.parametrize('damage', ['none', 'remote_file', 'missing_sha', 'local_manifest', 'remote_manifest'])
def test_explicit_legacy_attestation(settings, damage):
    api = legacy(settings)
    path = p._manifest_path(settings, 'r1')
    manifest = load_manifest(path)
    sha = manifest['hugging_face_revisions'][0]['commit_sha']
    if damage == 'remote_file':
        api.history[('owner/data', sha)]['README.md'] = b'tampered'
    elif damage == 'remote_manifest':
        api.history[('owner/data', sha)]['manifests/release.json'] = b'{}'
    elif damage == 'local_manifest':
        manifest['coverage']['complete'] = False
        dump_manifest(manifest, path)
    elif damage == 'missing_sha':
        with StateStore(settings.state_db) as store:
            row = store.get_release('r1')
            for event in row['guard_evidence']:
                event['evidence'].pop('commit_sha', None)
            store._connection.execute('UPDATE releases SET guard_evidence=? WHERE release_id=?',
                                      (json.dumps(row['guard_evidence']), 'r1'))
    if damage == 'none':
        assert p.attest_legacy(settings, release_id='r1', api=api)['attested'] is True
        with StateStore(settings.state_db) as store:
            assert p._trusted_published_manifest(settings, store, 'r1') == manifest
            guard = next(e['evidence'] for e in store.get_release('r1')['guard_evidence'] if e['state'] == 'published')
            assert guard['legacy_attestation']['method'] == 'legacy-remote-attestation'
            assert guard['legacy_attestation']['commit_sha'] == sha
        assert {name for repo, revision, name in api.downloads if revision == sha} >= {e['path'] for e in manifest['files']}
    else:
        with pytest.raises(ValueError):
            p.attest_legacy(settings, release_id='r1', api=api)
        with StateStore(settings.state_db) as store:
            guard = next(e['evidence'] for e in store.get_release('r1')['guard_evidence'] if e['state'] == 'published')
            assert 'published_manifest_sha256' not in guard


@pytest.mark.parametrize('damage', ['staging', 'raw'])
def test_attestation_rejects_candidate_authority(settings, damage):
    from kapa_runtime.releases.manifest import dumps_manifest
    api = legacy(settings)
    path = p._manifest_path(settings, 'r1')
    local = load_manifest(path)
    sha = local['hugging_face_revisions'][0]['commit_sha']
    remote = json.loads(api.history[('owner/data', sha)]['manifests/release.json'])
    if damage == 'staging':
        remote['staging_revision'] = local['staging_revision'] = {
            'repo_id': 'owner/forged-staging', 'commit_sha': 'c' * 40}
    else:
        entry = {'path': 'raw/envelope.xml', 'sha256': hashlib.sha256(b'<safe/>').hexdigest(), 'rows': 0}
        remote['files'].append(entry)
        local['files'].append(entry)
        api.history[('owner/data', sha)]['raw/envelope.xml'] = b'<safe/>'
    remote['files'].sort(key=lambda f: f['path'])
    payload = dumps_manifest(remote).encode()
    api.history[('owner/data', sha)]['manifests/release.json'] = payload
    next(f for f in local['files'] if f['path'] == 'manifests/release.json')['sha256'] = hashlib.sha256(payload).hexdigest()
    local['files'].sort(key=lambda f: f['path'])
    dump_manifest(local, path)
    with pytest.raises(ValueError):
        p.attest_legacy(settings, release_id='r1', api=api)
    with StateStore(settings.state_db) as store:
        guard = next(e['evidence'] for e in store.get_release('r1')['guard_evidence'] if e['state'] == 'published')
        assert 'published_manifest_sha256' not in guard


@pytest.mark.parametrize('revision', [None, [], {}, {'repo_id': 'owner/data', 'commit_sha': None},
    {'repo_id': '../escape', 'commit_sha': 'a' * 40},
    {'repo_id': 'owner/data', 'commit_sha': 'a' * 40, 'extra': True}])
def test_staging_revision_shape(settings, revision):
    from kapa_runtime.releases.manifest import validate_manifest
    collected(settings)
    p.build(settings, release_id='r1')
    manifest = load_manifest(p._manifest_path(settings, 'r1'))
    manifest['staging_revision'] = revision
    with pytest.raises(ValueError, match='Invalid staging revision'):
        validate_manifest(manifest)


@pytest.mark.parametrize('state', ['validated', 'uploaded-candidate'])
def test_query_binds_manifest_before_legacy_replay(settings, monkeypatch, state):
    collected(settings)
    p.build(settings, release_id='r1')
    if state == 'uploaded-candidate':
        with StateStore(settings.state_db) as store:
            store.transition_release('r1', state, {'private_verified': True, 'namespace_verified': True,
                'allowlist_verified': True, 'repo_id': 'owner/data', 'commit_sha': 'b' * 40})
    path = p._manifest_path(settings, 'r1')
    manifest = load_manifest(path)
    manifest['data_schema_version'] = 'kapa-runtime-data/1'
    dump_manifest(manifest, path)
    def replay(*args, **kwargs):
        pytest.fail('unbound schema reached replay')
    monkeypatch.setattr(p, '_verify_complete', replay)
    with pytest.raises(p.PipelineError, match='query_release_output_unverified'):
        p.query(settings, release_id='r1', filters={})


@pytest.mark.parametrize('escape', [False, True])
def test_attestation_cache_symlinks(settings, monkeypatch, escape):
    api = legacy(settings)
    original = api.hf_hub_download
    def download(*args, **kwargs):
        path = Path(original(*args, **kwargs))
        blob = (settings.data_root if escape else Path(kwargs['cache_dir'])) / 'blobs' / hashlib.sha256(path.read_bytes()).hexdigest()
        blob.parent.mkdir(parents=True, exist_ok=True)
        blob.write_bytes(path.read_bytes())
        path.unlink()
        try:
            path.symlink_to(blob)
        except OSError:
            pytest.skip('symlink privilege unavailable')
        return str(path)
    monkeypatch.setattr(api, 'hf_hub_download', download)
    if escape:
        with pytest.raises(ValueError, match='inside cache'):
            p.attest_legacy(settings, release_id='r1', api=api)
    else:
        assert p.attest_legacy(settings, release_id='r1', api=api)['attested']


@pytest.mark.parametrize('escape', [False, True])
def test_download_resolved_target_without_symlink_privilege(tmp_path, monkeypatch, escape):
    from kapa_runtime.releases.hf import download_bytes
    cache = tmp_path / 'cache'
    cache.mkdir()
    blob = (tmp_path if escape else cache) / 'blob'
    blob.write_bytes(b'verified bytes')
    returned = cache / 'snapshot-link'
    resolve = Path.resolve
    def resolver(self, *args, **kwargs):
        return blob if self == returned else resolve(self, *args, **kwargs)
    monkeypatch.setattr(Path, 'resolve', resolver)
    class Api:
        def get_paths_info(self, repo_id, paths, **kwargs):
            from types import SimpleNamespace
            return [SimpleNamespace(path=paths[0], size=blob.stat().st_size)]
        def hf_hub_download(self, *args, **kwargs):
            assert kwargs['revision'] == 'a' * 40 and kwargs['force_download']
            return returned
    if escape:
        with pytest.raises(ValueError, match='inside cache'):
            download_bytes(Api(), 'owner/data', 'README.md', revision='a' * 40, cache_dir=cache, token='fake')
    else:
        assert download_bytes(Api(), 'owner/data', 'README.md', revision='a' * 40, cache_dir=cache, token='fake') == b'verified bytes'


def test_download_plain_file_size_bound(tmp_path, monkeypatch):
    from kapa_runtime.releases import hf
    cache = tmp_path / 'cache'
    cache.mkdir()
    blob = cache / 'blob'
    blob.write_bytes(b'12345')
    class Api:
        def get_paths_info(self, repo_id, paths, **kwargs):
            from types import SimpleNamespace
            return [SimpleNamespace(path=paths[0], size=blob.stat().st_size)]
        def hf_hub_download(self, *args, **kwargs):
            return blob
    assert hf.download_bytes(Api(), 'owner/data', 'README.md', revision='a' * 40,
                             cache_dir=cache, token='fake') == b'12345'
    monkeypatch.setattr(hf, 'MAX_DOWNLOAD_BYTES', 4)
    with pytest.raises(ValueError, match='bounded regular file'):
        hf.download_bytes(Api(), 'owner/data', 'README.md', revision='a' * 40,
                          cache_dir=cache, token='fake')


def test_attest_legacy_cli_is_explicit(settings, monkeypatch, capsys):
    from kapa_runtime import cli
    monkeypatch.setattr(cli.RuntimeSettings, 'load', lambda _: settings)
    calls = []
    def attest(settings, *, release_id):
        calls.append(release_id)
        return {'release_id': release_id, 'attested': True}
    monkeypatch.setattr(cli.pipeline, 'attest_legacy', attest)
    assert cli.main(['attest-legacy', '--release-id', 'r1']) == 0
    assert calls == ['r1']
    assert json.loads(capsys.readouterr().out)['attested'] is True
