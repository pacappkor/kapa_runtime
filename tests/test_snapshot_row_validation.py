# SPDX-License-Identifier: GPL-3.0-only
"""Hash/schema-valid files must still satisfy the historical row decoder."""
import hashlib
import json
import sqlite3

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from kapa_runtime.releases.manifest import build_manifest, dump_manifest, manifest_digest
from kapa_runtime.storage.active import ActiveSnapshotError, search_active
from kapa_runtime.storage.parquet import read_partition, schema_for
from kapa_runtime.storage.snapshot_query import SnapshotIntegrityError
from test_cumulative_query import active_fixture


INVALID_VALUES = [('extra', '{invalid'), ('match_status', 'invalid'),
                  ('first_seen_at', None), ('last_seen_at', None)]


def bind_manifest(settings, manifest):
    dump_manifest(manifest, settings.active_manifest)
    with sqlite3.connect(settings.state_db) as con:
        con.execute('UPDATE releases SET guard_evidence=?', (json.dumps([
            {'state': 'published', 'evidence': {'commit_reread': True,
             'published_manifest_sha256': manifest_digest(manifest)}}]),))


@pytest.mark.parametrize('field,value', INVALID_VALUES)
@pytest.mark.parametrize('legacy', [False, True], ids=['v2', 'v1'])
def test_initial_verification_rejects_invalid_rows(tmp_path, monkeypatch, field, value, legacy):
    settings, manifest = active_fixture(tmp_path)
    root = settings.snapshots_dir / 'A'
    entries = [f for p in manifest['selected_partitions'] for f in p['files']]
    if legacy:
        manifest = build_manifest(release_id='A', files=manifest['files'] + entries,
            parser_version='fixture/1', state='published',
            github_revisions=manifest['github_revisions'],
            hugging_face_revisions=manifest['hugging_face_revisions'],
            quality={'passed': True}, coverage={})
        manifest['data_schema_version'] = 'kapa-runtime-data/1'
        for entry in entries:
            path = root / entry['path']
            table = pq.ParquetFile(path).read().cast(schema_for('kapa-runtime-data/1'))
            pq.write_table(table, path)
            entry['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        # build_manifest may copy its file entries.
        manifest['files'] = [manifest['files'][0]] + entries
    bind_manifest(settings, manifest)
    assert search_active(settings, {})[1]['total'] == 4
    victim = next(f for f in entries if f['rows'])
    path = root / victim['path']
    table = pq.ParquetFile(path).read()
    index = table.schema.get_field_index(field)
    table = table.set_column(index, table.schema.field(index),
                             pa.array([value] * table.num_rows, type=table.schema.field(index).type))
    pq.write_table(table, path)
    victim['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    bind_manifest(settings, manifest)
    with pytest.raises(Exception):
        read_partition(path, schema_version=manifest['data_schema_version'])

    def forbidden(*args, **kwargs):
        pytest.fail('query must not start before full-row verification succeeds')

    monkeypatch.setattr('kapa_runtime.storage.active.TransactionQuery.search', forbidden)
    with pytest.raises(ActiveSnapshotError, match='^active_snapshot_unverified$'):
        search_active(settings, {})


@pytest.mark.parametrize('legacy', [False, True], ids=['v2', 'v1'])
def test_query_decode_failure_is_integrity_error(tmp_path, monkeypatch, legacy):
    from kapa_runtime.storage.query import TransactionQuery
    from test_cumulative_query import fixture
    manifest = fixture(tmp_path)
    query = TransactionQuery(tmp_path, manifest=None if legacy else manifest)

    def broken(*args, **kwargs):
        raise ValueError('row decode failed')

    monkeypatch.setattr('kapa_runtime.storage.query.' +
                        ('read_partition' if legacy else 'decode_record'), broken)
    with pytest.raises(SnapshotIntegrityError, match='snapshot_row_unverified'):
        query.search({})


def test_query_decode_failure_maps_to_active_error(tmp_path, monkeypatch):
    settings, _ = active_fixture(tmp_path)

    def broken(*args, **kwargs):
        raise ValueError('row decode failed after successful verification')

    monkeypatch.setattr('kapa_runtime.storage.query.decode_record', broken)
    with pytest.raises(ActiveSnapshotError, match='^active_snapshot_unverified$'):
        search_active(settings, {})
