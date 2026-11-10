# SPDX-License-Identifier: GPL-3.0-only
import hashlib
from pathlib import Path

import pytest

from kapa_runtime import pipeline as p
from kapa_runtime.credentials import Secret, redact
from kapa_runtime.releases.hf import _HubWriter, PublishError, scan_secret_payload
from kapa_runtime.releases.manifest import load_manifest
from kapa_runtime.storage.metadata import read_json
from kapa_runtime.storage.state import StateStore
from test_hf_publish import FakeSecret
from test_pipeline import settings, collected
from test_public_promotion import MultiRepoApi


def test_private_failed_inventory_is_removed(settings):
    collected(settings)
    p.build(settings, release_id='r1')
    api = MultiRepoApi()
    original = api.hf_hub_download
    def fail(*args, **kwargs):
        raise OSError('synthetic verify failure')
    api.hf_hub_download = fail
    with pytest.raises(p.PipelineError):
        p.publish(settings, release_id='r1', api=api)
    repo = api.repos['owner/data']
    repo['files']['data/quarantined-month.parquet'] = b'stale'
    api.history[('owner/data', repo['sha'])] = dict(repo['files'])
    api.hf_hub_download = original
    original_commit = api.create_commit
    def commit(repo_id, *, parent_commit, **kwargs):
        assert parent_commit == api.repos[repo_id]['sha']
        return original_commit(repo_id, **kwargs)
    api.create_commit = commit
    collected(settings, 'r2')
    p.build(settings, release_id='r2')
    assert p.publish(settings, release_id='r2', api=api)['state'] == 'published'
    assert 'data/quarantined-month.parquet' not in repo['files']


def test_private_exact_inventory_preserves_only_trusted_history(tmp_path):
    api = MultiRepoApi()
    api.create_repo('owner/data', private=True)
    pub = _HubWriter(api, 'owner', 'data', role='market', secret_values=lambda: [FakeSecret()])
    pub._replace_files({'old.json': b'old', 'quarantined.json': b'stale', '.gitattributes': b'*.json text'}, preserve_attributes=False)
    (tmp_path / 'new.json').write_bytes(b'new')
    with StateStore(tmp_path / 'state.sqlite') as store:
        pub.upload_candidate(tmp_path, ['new.json'], exact_inventory=True, evidence_store=store,
            cache_dir=tmp_path / 'cache', previous_files=[{'path': 'old.json',
            'sha256': hashlib.sha256(b'old').hexdigest(), 'rows': 0}])
    assert api.repos['owner/data']['files'] == {'new.json': b'new', 'old.json': b'old', '.gitattributes': b'*.json text'}


@pytest.mark.parametrize('operation', ['build', 'query'])
@pytest.mark.parametrize('linked_ancestor', [False, True])
def test_collection_metadata_checked(settings, monkeypatch, operation, linked_ancestor):
    collected(settings)
    target = settings.staging_dir / 'r1' / 'collection.json'
    blocked = target.parent if linked_ancestor else target
    original_link, original_read = Path.is_symlink, Path.read_text
    def linked(path):
        return path == blocked or original_link(path)
    def read(path, *args, **kwargs):
        if path == target:
            pytest.fail('metadata followed before checked read')
        return original_read(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'is_symlink', linked)
    monkeypatch.setattr(Path, 'read_text', read)
    with pytest.raises(ValueError):
        if operation == 'build':
            p.build(settings, release_id='r1')
        else:
            p.query(settings, release_id='r1', filters={})


@pytest.mark.parametrize('name', ['collection.json', 'operator-policy.json', 'active.json', 'previous.json'])
def test_all_json_metadata_reparse_checked(tmp_path, monkeypatch, name):
    target = tmp_path / name
    target.write_text('{}')
    original = Path.stat
    def reparse(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path == target:
            from types import SimpleNamespace
            return SimpleNamespace(st_mode=result.st_mode, st_file_attributes=0x400)
        return result
    monkeypatch.setattr(Path, 'stat', reparse)
    with pytest.raises(ValueError, match='reparse'):
        read_json(target)


@pytest.mark.parametrize('secret', ['fake%2bcredential%2fvalue', 'fake%2Bcredential%2Fvalue'])
@pytest.mark.parametrize('payload', ['fake%2bcredential%2fvalue', 'fake%2Bcredential%2Fvalue', 'fake+credential/value'])
def test_canonical_secret_needles(secret, payload):
    assert redact('prefix ' + payload + ' suffix', [Secret(secret)]) == 'prefix *** suffix'
    with pytest.raises(PublishError, match='configured secret'):
        scan_secret_payload(payload.encode(), 'data.json', [Secret(secret)])
