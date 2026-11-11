# SPDX-License-Identifier: GPL-3.0-only
import copy
from datetime import date
import hashlib
import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from kapa_runtime.releases.manifest_v2 import coverage_for_partitions, KEY, key
from kapa_runtime.storage.parquet import SCHEMA, _row
from kapa_runtime.storage.query import TransactionQuery
from kapa_runtime.storage.snapshot_query import SnapshotInventory, BATCH_SIZE
from test_manifest_v2 import root, partitions
from test_parquet import record


def fixture(root_dir, *, size=3):
    manifest = root()
    manifest['files'] = []
    parts = []
    for region, month in [('11110', '202510'), ('26110', '202509')]:
        for p in partitions(region=region, month=month):
            kind = p['service_id'].rsplit('-', 1)[1]
            rows = [] if kind == 'rent' else [_row(record('same', observation_id=p['observation_id'],
                lawd_code=region, contract_date=date(int(month[:4]), int(month[4:]), 1),
                price_krw=10, cancellation_status='O' if i == 0 else None)) for i in range(size)]
            path = root_dir / p['files'][0]['path']
            path.parent.mkdir(parents=True, exist_ok=True)
            pq.write_table(pa.Table.from_pylist(rows, schema=SCHEMA), path)
            p.update(rows=len(rows), status='success' if rows else 'zero')
            p['files'][0].update(rows=len(rows), sha256=hashlib.sha256(path.read_bytes()).hexdigest())
            parts.append(p)
    parts.sort(key=key)
    manifest.update(selected_partitions=parts,
        batch_scope={'declared_pairs': [{'lawd_code': '11110', 'deal_ymd': '202510'},
                                         {'lawd_code': '26110', 'deal_ymd': '202509'}]})
    declared = [{k: p[k] for k in KEY} for p in parts]
    declared += [{k: p[k] for k in KEY} for p in partitions(region='41110', month='202508')]
    manifest['coverage'] = coverage_for_partitions(parts, sorted(declared, key=key))
    return manifest


def test_sums_duplicates_cancellations_and_exact_coverage(tmp_path):
    manifest = fixture(tmp_path)
    query = TransactionQuery(tmp_path, manifest=manifest)
    result = query.search({})
    assert result['total'] == 4
    assert sum(r['price_krw'] for r in result['rows']) == 40
    assert result['excluded']['count'] == 2
    assert len(result['coverage']['observed']) == 4
    assert len(result['coverage']['zero']) == 2
    assert len(result['coverage']['not_collected']) == 2
    assert len(result['partition_provenance']) == 4
    assert query.search({'include_cancelled': True})['total'] == 6
    assert query.search({'lawd_code': '26110', 'contract_date_to': '2025-09-30'})['total'] == 2
    assert query.search({'contract_date_from': '2025-10-01'})['total'] == 2
    assert query.search({'transaction_type': 'rent'})['total'] == 0
    assert query.search({'lawd_code': '00000'})['total'] == 0


def test_archive_never_read_or_globbed(tmp_path, monkeypatch):
    manifest = fixture(tmp_path)
    (tmp_path / 'archive.parquet').write_bytes(b'not parquet: must never be read')
    def forbidden(*args, **kwargs):
        pytest.fail('recursive glob forbidden for v2')
    monkeypatch.setattr(type(tmp_path), 'rglob', forbidden)
    SnapshotInventory(tmp_path, manifest).verify()
    assert TransactionQuery(tmp_path, manifest=manifest).search({})['total'] == 4


def test_cursor_binds_provenance_and_filters(tmp_path):
    manifest = fixture(tmp_path)
    cursor = TransactionQuery(tmp_path, manifest=manifest).search({}, limit=1)['next_cursor']
    assert TransactionQuery(tmp_path, manifest=manifest).search({}, limit=1, cursor=cursor)['total'] == 4
    changed = copy.deepcopy(manifest)
    changed['selected_partitions'][0]['evidence_digest'] = 'f' * 64
    for m, filters in [(changed, {}), (manifest, {'lawd_code': '26110'})]:
        with pytest.raises(ValueError, match='cursor'):
            TransactionQuery(tmp_path, manifest=m).search(filters, cursor=cursor)


def test_shard_digest_and_bounded_batches(tmp_path, monkeypatch):
    manifest = fixture(tmp_path, size=BATCH_SIZE * 2 + 1)
    shard = dict(schema_version='kapa-runtime-coverage/1',
                 license='SPDX-License-Identifier: GPL-3.0-only',
                 selected_partitions=manifest['selected_partitions'], coverage=manifest['coverage'])
    payload = json.dumps(shard).encode()
    digest = hashlib.sha256(payload).hexdigest()
    name = f'manifests/coverage/{digest}.json'
    path = tmp_path / name
    path.parent.mkdir(parents=True)
    path.write_bytes(payload)
    count = len(shard['selected_partitions']) + sum(len(shard['coverage'][n]) for n in ('declared', 'observed', 'zero', 'not_collected'))
    manifest.update(selected_partitions=[], coverage=coverage_for_partitions([], []),
                    coverage_shards=[dict(path=name, sha256=digest, entries=count)])
    sizes = []
    original = pq.ParquetFile.iter_batches
    def observed(self, *args, **kwargs):
        for batch in original(self, *args, **kwargs):
            sizes.append(batch.num_rows)
            yield batch
    monkeypatch.setattr(pq.ParquetFile, 'iter_batches', observed)
    result = TransactionQuery(tmp_path, manifest=manifest).search({}, limit=1)
    assert result['total'] == BATCH_SIZE * 4
    assert sizes and max(sizes) <= BATCH_SIZE
    path.write_bytes(payload + b' ')
    with pytest.raises(ValueError, match='digest'):
        SnapshotInventory(tmp_path, manifest)


def test_partition_metadata_is_checked_before_pruning(tmp_path):
    manifest = fixture(tmp_path)
    p = next(p for p in manifest['selected_partitions'] if p['status'] == 'success')
    path = tmp_path / p['files'][0]['path']
    pq.write_table(pa.Table.from_pylist([_row(record('wrong'))] * 3, schema=SCHEMA), path)
    p['files'][0]['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match='partition_metadata'):
        SnapshotInventory(tmp_path, manifest).verify()


def test_active_coverage_pagination_trust_and_offline(tmp_path, monkeypatch):
    import sqlite3
    import socket
    from kapa_runtime.config import RuntimeSettings
    from kapa_runtime.releases.manifest import dump_manifest, manifest_digest
    from kapa_runtime.storage.active import coverage_active, search_active, ActiveSnapshotError
    settings = RuntimeSettings(tmp_path, tmp_path / 'data')
    directory = settings.snapshots_dir / 'A'
    manifest = fixture(directory)
    (directory / 'README.md').write_bytes(b'fixture')
    manifest['files'] = [dict(path='README.md', sha256=hashlib.sha256(b'fixture').hexdigest(), rows=0)]
    manifest.update(state='published', quality={'passed': True, 'legal_verified': False},
        github_revisions={'pacappkor/kapa_runtime': 'a' * 40,
                          'pacappkor/kapa_skills': 'b' * 40,
                          'pacappkor/kapa_knowledge': 'c' * 40},
        hugging_face_revisions=[{'repo_id': 'fixture/data', 'commit_sha': 'd' * 40}])
    settings.manifests_dir.mkdir(parents=True)
    dump_manifest(manifest, settings.active_manifest)
    settings.state_db.parent.mkdir(parents=True)
    with sqlite3.connect(settings.state_db) as con:
        con.execute('CREATE TABLE releases (release_id TEXT, guard_evidence TEXT)')
        con.execute('INSERT INTO releases VALUES (?, ?)', ('A', json.dumps([
            {'state': 'published', 'evidence': {'commit_reread': True,
              'published_manifest_sha256': manifest_digest(manifest)}}])))
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **k: pytest.fail('network'))
    _, first = coverage_active(settings, limit=1)
    assert first['total'] == 6
    assert len(first['rows']) == 1
    _, second = coverage_active(settings, limit=1, cursor=first['next_cursor'])
    assert second['rows'] != first['rows']
    assert second['verification']['scope'] == 'full-current-inventory-pre-post'
    assert search_active(settings, {})[1]['total'] == 4
    with pytest.raises(ValueError, match='cursor'):
        coverage_active(settings, {'lawd_code': '26110'}, cursor=first['next_cursor'])
    with pytest.raises(ValueError):
        coverage_active(settings, {'contract_date_from': 'bad'})
    # Even an unqueried current partition remains in the full trust verification.
    victim = directory / manifest['selected_partitions'][-1]['files'][0]['path']
    victim.write_bytes(b'tampered')
    with pytest.raises(ActiveSnapshotError):
        search_active(settings, {'lawd_code': '11110'})
