# SPDX-License-Identifier: GPL-3.0-only
import hashlib
import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest


pytestmark = pytest.mark.slow

from kapa_runtime import pipeline as p
from kapa_runtime.releases.manifest import load_manifest, dump_manifest
from kapa_runtime.storage.parquet import schema_for
from test_pipeline import settings, collected
from test_public_promotion import MultiRepoApi
from publication_fixture import seal_publication


def test_historical_release_query_replays_v1(settings):
    collected(settings)
    p.build(settings, release_id='r1')
    api = MultiRepoApi()
    p.publish(settings, release_id='r1', api=api)
    manifest_path = p._manifest_path(settings, 'r1')
    manifest = load_manifest(manifest_path)
    manifest['data_schema_version'] = 'kapa-runtime-data/1'
    root = settings.staging_dir / 'r1'
    metadata_path = root / 'collection.json'
    meta = json.loads(metadata_path.read_text())
    for entry in manifest['files']:
        if not entry['path'].endswith('.parquet'):
            continue
        for base in [root, settings.snapshots_dir / 'r1']:
            path = base / entry['path']
            table = pq.read_table(pa.BufferReader(path.read_bytes()))
            pq.write_table(table.replace_schema_metadata(schema_for('kapa-runtime-data/1').metadata), path)
        entry['sha256'] = hashlib.sha256((root / entry['path']).read_bytes()).hexdigest()
        for completed in meta['completed'].values():
            for file in completed['files']:
                if file['path'] == entry['path']:
                    file['sha256'] = entry['sha256']
    metadata_path.write_text(json.dumps(meta))
    dump_manifest(manifest, manifest_path)
    seal_publication(settings, manifest)
    assert p.query(settings, release_id='r1', filters={})['total'] == 2


@pytest.mark.parametrize('active', [False, True])
def test_mutable_historical_manifest_not_retention_authority(settings, active):
    collected(settings)
    p.build(settings, release_id='r1')
    api = MultiRepoApi()
    p.publish(settings, release_id='r1', api=api)
    path = p._manifest_path(settings, 'r1')
    old = load_manifest(path)
    name = 'schemas/injected.json'
    payload = b'{"unvalidated":true}'
    old['files'].append({'path': name, 'sha256': hashlib.sha256(payload).hexdigest(), 'rows': 0})
    old['files'].sort(key=lambda f: f['path'])
    dump_manifest(old, path)
    snapshot = settings.snapshots_dir / 'r1' / name
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_bytes(payload)
    if active:
        dump_manifest(old, settings.active_manifest)
    repo = api.repos['owner/data']
    repo['files'][name] = b'old remote bytes'
    api.history[('owner/data', repo['sha'])] = dict(repo['files'])
    with pytest.raises(p.PipelineError, match='published_manifest_untrusted'):
        p.activate(settings, release_id='r1', api=api)
    with pytest.raises(p.PipelineError, match='published_manifest_untrusted'):
        p.query(settings, release_id='r1', filters={})
    collected(settings, 'r2')
    p.build(settings, release_id='r2')
    before = len(api.commits)
    if active:
        with pytest.raises(p.PipelineError, match='published_manifest_untrusted'):
            p.publish(settings, release_id='r2', api=api)
        assert len(api.commits) == before
    else:
        assert p.publish(settings, release_id='r2', api=api)['state'] == 'published'
        assert name not in repo['files']


def test_rollback_pointer_requires_published_digest(settings):
    api = MultiRepoApi()
    for release in ['r1', 'r2']:
        collected(settings, release)
        p.build(settings, release_id=release)
        p.publish(settings, release_id=release, api=api)
        p.activate(settings, release_id=release, api=api)
    path = settings.active_manifest.with_name('previous.json')
    old = load_manifest(path)
    old['coverage']['complete'] = False
    dump_manifest(old, path)
    with pytest.raises(p.PipelineError, match='published_manifest_untrusted'):
        p.rollback(settings, api=api)
