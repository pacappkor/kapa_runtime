# SPDX-License-Identifier: GPL-3.0-only
import hashlib
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest


pytestmark = pytest.mark.slow
from publication_fixture import seal_publication

from kapa_runtime import pipeline as p
from kapa_runtime.releases.manifest import load_manifest, dump_manifest
from kapa_runtime.storage import metadata
from kapa_runtime.storage.state import StateStore
from test_pipeline import settings, collected
from test_public_promotion import MultiRepoApi


@pytest.mark.parametrize('mode', [stat.S_IFIFO, stat.S_IFCHR])
def test_metadata_rejects_special_file_before_open(tmp_path, monkeypatch, mode):
    path = tmp_path / 'metadata.json'
    path.write_text('{}')
    original = os.lstat
    def special(target, *a, **k):
        if Path(target) == path:
            return SimpleNamespace(st_mode=mode, st_size=0, st_reparse_tag=0, st_file_attributes=0)
        return original(target, *a, **k)
    monkeypatch.setattr(os, 'lstat', special)
    monkeypatch.setattr(os, 'open', lambda *a, **k: pytest.fail('opened special file'))
    with pytest.raises(ValueError, match='regular file'):
        metadata.read_json(path)


def test_metadata_checks_opened_descriptor_identity(tmp_path, monkeypatch):
    path = tmp_path / 'metadata.json'
    other = tmp_path / 'other.json'
    path.write_text('{}')
    other.write_text('{}')
    original = os.open
    monkeypatch.setattr(os, 'open', lambda target, flags: original(other, flags))
    with pytest.raises(ValueError, match='changed during open'):
        metadata.read_json(path)


def test_metadata_cap_and_open_flags(tmp_path, monkeypatch):
    path = tmp_path / 'metadata.json'
    path.write_text('{"safe":true}')
    original = os.open
    def opened(target, flags):
        assert flags & getattr(os, 'O_NOFOLLOW', 0) == getattr(os, 'O_NOFOLLOW', 0)
        return original(target, flags)
    monkeypatch.setattr(os, 'open', opened)
    assert metadata.read_json(path) == {'safe': True}
    monkeypatch.setattr(metadata, 'MAX_METADATA_BYTES', 2)
    with pytest.raises(ValueError, match='bounded'):
        metadata.read_json(path)


@pytest.mark.parametrize('local,active', [(True, False), (False, False), (False, True), (True, True)])
def test_retained_partition_repair_or_removal(settings, local, active):
    collected(settings)
    p.build(settings, release_id='r1')
    api = MultiRepoApi()
    p.publish(settings, release_id='r1', api=api)
    old_path = p._manifest_path(settings, 'r1')
    old = load_manifest(old_path)
    name = 'schemas/historic.json'
    content = b'{"trusted":true}'
    old['files'].append({'path': name, 'sha256': hashlib.sha256(content).hexdigest(), 'rows': 0})
    old['files'].sort(key=lambda f: f['path'])
    dump_manifest(old, old_path)
    seal_publication(settings, old)
    if local:
        target = settings.snapshots_dir / 'r1' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    if active:
        dump_manifest(old, settings.active_manifest)
    repo = api.repos['owner/data']
    repo['files'][name] = b'quarantined candidate bytes'
    api.history[('owner/data', repo['sha'])] = dict(repo['files'])
    collected(settings, 'r2')
    p.build(settings, release_id='r2')
    before = len(api.commits)
    if active and not local:
        with pytest.raises(ValueError, match="active_retained_partition_unrecoverable"):
            p.publish(settings, release_id='r2', api=api)
        assert len(api.commits) == before
        assert repo['files'][name] == b'quarantined candidate bytes'
    else:
        result = p.publish(settings, release_id='r2', api=api)
        assert result['state'] == 'published'
        if local:
            assert repo['files'][name] == content
            assert result['warnings'] == []
        else:
            assert name not in repo['files']
            assert result['warnings'] == [{'code': 'retained_partition_removed', 'release_id': 'r1', 'path': name}]
    with StateStore(settings.state_db) as store:
        assert store.get_release('r1')['fully_served'] is (local or active)
