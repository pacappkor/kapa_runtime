# SPDX-License-Identifier: GPL-3.0-only
import hashlib

import pyarrow as pa
import pyarrow.parquet as pq
import pytest


pytestmark = pytest.mark.slow
from publication_fixture import seal_publication

from kapa_runtime import pipeline as p
from kapa_runtime.releases.manifest import load_manifest, dump_manifest
from kapa_runtime.storage.parquet import SCHEMA, schema_for
from test_pipeline import settings, collected
from test_public_promotion import MultiRepoApi


@pytest.mark.parametrize('repair', [False, True])
def test_active_v1_retained_by_v2_publication(settings, repair):
    collected(settings)
    p.build(settings, release_id='r1')
    api = MultiRepoApi()
    p.publish(settings, release_id='r1', api=api)
    manifest_path = p._manifest_path(settings, 'r1')
    old = load_manifest(manifest_path)
    old['data_schema_version'] = 'kapa-runtime-data/1'
    partition = next(f for f in old['files'] if f['path'].endswith('.parquet'))
    # Give the historical partition its own month so the v2 candidate retains it.
    previous_path = partition['path']
    partition['path'] = previous_path.replace('contract_month=10', 'contract_month=9')
    snapshot = settings.snapshots_dir / 'r1'
    for entry in old['files']:
        if entry['path'].endswith('.parquet'):
            path = snapshot / (previous_path if entry is partition else entry['path'])
            table = pq.read_table(pa.BufferReader(path.read_bytes())).replace_schema_metadata(schema_for('kapa-runtime-data/1').metadata)
            target = snapshot / entry['path']
            target.parent.mkdir(parents=True, exist_ok=True)
            pq.write_table(table, target)
            if target != path:
                path.unlink()
            payload = target.read_bytes()
            entry['sha256'] = hashlib.sha256(payload).hexdigest()
            api.repos['owner/data']['files'][entry['path']] = payload
    dump_manifest(old, manifest_path)
    seal_publication(settings, old)
    dump_manifest(old, settings.active_manifest)
    trusted = (snapshot / partition['path']).read_bytes()
    if repair:
        api.repos['owner/data']['files'][partition['path']] = b'quarantined overwrite'
    api.history[('owner/data', api.repos['owner/data']['sha'])] = dict(api.repos['owner/data']['files'])
    p._verify_local(snapshot, old)
    collected(settings, 'r2')
    p.build(settings, release_id='r2')
    assert p.publish(settings, release_id='r2', api=api)['state'] == 'published'
    assert api.repos['owner/data']['files'][partition['path']] == trusted


def test_unknown_owned_schema_refused(settings):
    collected(settings)
    p.build(settings, release_id='r1')
    api = MultiRepoApi()
    p.publish(settings, release_id='r1', api=api)
    path = p._manifest_path(settings, 'r1')
    old = load_manifest(path)
    old['data_schema_version'] = 'kapa-runtime-data/unknown'
    dump_manifest(old, path)
    seal_publication(settings, old)
    with pytest.raises(ValueError, match='unsupported data schema'):
        p._verify_local(settings.snapshots_dir / 'r1', old)
    collected(settings, 'r2')
    p.build(settings, release_id='r2')
    before = len(api.commits)
    with pytest.raises(ValueError, match='unsupported data schema'):
        p.publish(settings, release_id='r2', api=api)
    assert len(api.commits) == before


def test_frozen_schemas_and_v1_string_scan(tmp_path):
    from kapa_runtime.credentials import Secret
    from kapa_runtime.releases.hf import scan_secret_payload, PublishError
    assert schema_for('kapa-runtime-data/2').equals(SCHEMA, check_metadata=True)
    row = {name: None for name in SCHEMA.names}
    row['extra'] = '{"legacyUnknown":"TESTKEY-not-real"}'
    path = tmp_path / 'old.parquet'
    pq.write_table(pa.Table.from_pylist([row], schema=schema_for('kapa-runtime-data/1')), path, compression='gzip')
    with pytest.raises(PublishError):
        scan_secret_payload(path.read_bytes(), path.name, [Secret('TESTKEY-not-real')], schema_version='kapa-runtime-data/1')
    with pytest.raises(PublishError):
        scan_secret_payload(path.read_bytes(), path.name, [Secret('other')])


@pytest.mark.parametrize('local,active', [(True, False), (False, False), (False, True)])
def test_public_staging_retained_repair(settings, local, active):
    from dataclasses import replace
    settings = replace(settings, hf_visibilities={'market': 'public'})
    collected(settings)
    p.build(settings, release_id='r1')
    api = MultiRepoApi()
    p.publish(settings, release_id='r1', api=api)
    manifest_path = p._manifest_path(settings, 'r1')
    old = load_manifest(manifest_path)
    name = 'schemas/previous.json'
    payload = b'{"trusted":true}'
    old['files'].append({'path': name, 'sha256': hashlib.sha256(payload).hexdigest(), 'rows': 0})
    old['files'].sort(key=lambda f: f['path'])
    dump_manifest(old, manifest_path)
    seal_publication(settings, old)
    if active:
        dump_manifest(old, settings.active_manifest)
    if local:
        target = settings.snapshots_dir / 'r1' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    repo = api.repos['owner/data-staging']
    repo['files'][name] = b'quarantined overwrite'
    api.history[('owner/data-staging', repo['sha'])] = dict(repo['files'])
    collected(settings, 'r2')
    p.build(settings, release_id='r2')
    before = len(api.commits)
    # Synthetic ownership has no matching public baseline. Public retention is
    # now verified before staging writes, so fail without mutating either repo
    # or recording a staging removal that could conceal the invalid baseline.
    with pytest.raises(p.PipelineError, match='public_restore_inventory_unverified'):
        p.publish(settings, release_id='r2', api=api)
    assert len(api.commits) == before
    assert repo['files'][name] == b'quarantined overwrite'
    from kapa_runtime.storage.state import StateStore
    with StateStore(settings.state_db) as store:
        assert store.get_release('r1')['fully_served'] is True
