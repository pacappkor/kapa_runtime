# SPDX-License-Identifier: GPL-3.0-only
import hashlib

import pyarrow as pa
import pyarrow.parquet as pq
import pytest


pytestmark = pytest.mark.slow
from publication_fixture import seal_publication

from kapa_runtime import pipeline as p
from kapa_runtime.releases.manifest import load_manifest, dump_manifest
from kapa_runtime.storage.parquet import schema_for
from kapa_runtime.storage.state import StateStore
from test_pipeline import settings, collected
from test_public_promotion import MultiRepoApi


def publish(settings, api, release):
    collected(settings, release)
    p.build(settings, release_id=release)
    p.publish(settings, release_id=release, api=api)


def test_active_v1_query_compatible_projection_and_mixed_refusal(settings):
    api = MultiRepoApi()
    publish(settings, api, 'r1')
    manifest = load_manifest(p._manifest_path(settings, 'r1'))
    manifest['data_schema_version'] = 'kapa-runtime-data/1'
    snapshot = settings.snapshots_dir / 'r1'
    for entry in manifest['files']:
        if entry['path'].endswith('.parquet'):
            path = snapshot / entry['path']
            table = pq.read_table(pa.BufferReader(path.read_bytes()))
            pq.write_table(table.replace_schema_metadata(schema_for('kapa-runtime-data/1').metadata), path)
            entry['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    dump_manifest(manifest, settings.active_manifest)
    seal_publication(settings, manifest)
    result = p.query(settings, filters={})
    assert result['total'] == 2
    assert all('extra' in row and 'price_krw' in row for row in result['rows'])
    entry = next(f for f in manifest['files'] if f['path'].endswith('.parquet'))
    path = snapshot / entry['path']
    table = pq.read_table(pa.BufferReader(path.read_bytes()))
    pq.write_table(table.replace_schema_metadata(schema_for('kapa-runtime-data/2').metadata), path)
    entry['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    dump_manifest(manifest, settings.active_manifest)
    with pytest.raises(ValueError):
        p.query(settings, filters={})


@pytest.mark.parametrize('active_owner', [True, False])
def test_retained_owner_active_else_latest_publish(settings, active_owner):
    api = MultiRepoApi()
    name = 'schemas/shared.json'
    for release, payload in [('z_old', b'old'), ('a_new', b'new')]:
        publish(settings, api, release)
        path = p._manifest_path(settings, release)
        manifest = load_manifest(path)
        manifest['files'].append({'path': name, 'sha256': hashlib.sha256(payload).hexdigest(), 'rows': 0})
        manifest['files'].sort(key=lambda f: f['path'])
        dump_manifest(manifest, path)
        seal_publication(settings, manifest)
        target = settings.snapshots_dir / release / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    if active_owner:
        dump_manifest(load_manifest(p._manifest_path(settings, 'z_old')), settings.active_manifest)
    expected = b'old' if active_owner else b'new'
    repo = api.repos['owner/data']
    repo['files'][name] = b'new' if active_owner else b'old'
    api.history[('owner/data', repo['sha'])] = dict(repo['files'])
    with StateStore(settings.state_db) as store:
        selected = [e for e in p._previous_repo_files(settings, store, 'owner/data') if e['path'] == name]
    assert len(selected) == 1 and selected[0]['sha256'] == hashlib.sha256(expected).hexdigest()
    publish(settings, api, 'r3')
    assert repo['files'][name] == expected
    # The verifier must reject the other historical owner's bytes as well.
    latest = load_manifest(p._manifest_path(settings, 'r3'))
    ref = latest['hugging_face_revisions'][0]['commit_sha']
    api.history[('owner/data', ref)][name] = b'new' if active_owner else b'old'
    with StateStore(settings.state_db) as store:
        report = p._publisher(settings, api).verify(ref, latest, settings.cache_dir / 'owner-check',
            previous_files=p._previous_repo_files(settings, store, 'owner/data'), evidence_store=store)
    assert not report.passed
