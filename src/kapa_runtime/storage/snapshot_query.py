# SPDX-License-Identifier: GPL-3.0-only
"""Explicit, digest-bound cumulative inventory and bounded local verification."""
from __future__ import annotations

from datetime import date
import hashlib
import copy
from pathlib import Path

import pyarrow.dataset as ds
import pyarrow.parquet as pq

from ..releases.manifest import dumps_manifest, manifest_digest, relative_path
from ..releases.manifest_v2 import COVERAGE, KEY, resolve_snapshot

DEFAULT_METADATA_BUDGET = 256 * 1024 * 1024
from .metadata import read_metadata
from .parquet import _safe_path, schema_for

BATCH_SIZE = 4096
PROVENANCE_LIMIT = 100


class SnapshotIntegrityError(ValueError):
    """Local snapshot bytes no longer satisfy their verified inventory."""


def file_digest(path):
    _safe_path(path)
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def verify_files(root, entries, version, partitions=()):
    """Stream bytes and decoded batches; classify every verification failure."""
    try:
        _verify_files(root, entries, version, partitions)
    except Exception as exc:
        raise SnapshotIntegrityError(str(exc)) from exc


def _verify_files(root, entries, version, partitions):
    owners = {f['path']: p for p in partitions for f in p['files']}
    for entry in entries:
        path = Path(root) / relative_path(entry['path'])
        _safe_path(path)
        if file_digest(path) != entry['sha256']:
            raise ValueError('local_hash_mismatch')
        if path.suffix != '.parquet':
            continue
        parquet = pq.ParquetFile(path)
        if not parquet.schema_arrow.equals(schema_for(version), check_metadata=True):
            raise ValueError('schema_mismatch')
        count = 0
        owner = owners.get(entry['path'])
        columns = ['asset_type', 'transaction_type', 'lawd_code', 'contract_date', 'observation_id']
        for batch in parquet.iter_batches(batch_size=BATCH_SIZE, use_threads=False):
            count += batch.num_rows
            if owner is not None:
                for row in batch.select(columns).to_pylist():
                    month = row['contract_date']
                    if (row['asset_type'] != 'apartment' or
                            row['transaction_type'] != owner['service_id'].removeprefix('molit-apartment-') or
                            row['lawd_code'] != owner['lawd_code'] or
                            row['observation_id'] != owner['observation_id'] or
                            (month is not None and month.strftime('%Y%m') != owner['deal_ymd'])):
                        raise ValueError('partition_metadata_mismatch')
        if count != entry['rows']:
            raise ValueError('row_count_mismatch')


def overlaps(partition, filters):
    """Only prune on validated K metadata, never infer a Cartesian coverage."""
    if filters.get('asset_type', 'apartment') != 'apartment':
        return False
    for name in ('lawd_code', 'service_id'):
        if name in filters and filters[name] != partition[name]:
            return False
    if ('transaction_type' in filters and filters['transaction_type'] !=
            partition['service_id'].removeprefix('molit-apartment-')):
        return False
    for name, lower in (('contract_date_from', True), ('contract_date_to', False)):
        if name in filters:
            value = filters[name]
            if isinstance(value, str):
                value = date.fromisoformat(value)
            month = value.strftime('%Y%m')
            if (lower and partition['deal_ymd'] < month) or (not lower and partition['deal_ymd'] > month):
                return False
    return True


class SnapshotInventory:
    def __init__(self, root, manifest, *, metadata_budget=DEFAULT_METADATA_BUDGET,
                 root_metadata_bytes=None):
        self.root = Path(root)
        manifest = copy.deepcopy(manifest)
        self.manifest = manifest
        self.digest = manifest_digest(manifest)
        if type(metadata_budget) is not int or metadata_budget <= 0:
            raise ValueError('metadata_budget must be a positive integer')
        canonical_bytes = len(dumps_manifest(manifest).encode('utf-8'))
        self.metadata_bytes = max(canonical_bytes, root_metadata_bytes or 0)
        def check_budget():
            if self.metadata_bytes > metadata_budget:
                raise SnapshotIntegrityError('snapshot_metadata_budget_exceeded')
        check_budget()
        def load(name):
            path = self.root / relative_path(name)
            _safe_path(path)
            try:
                payload = read_metadata(path, as_bytes=True,
                                        max_bytes=metadata_budget - self.metadata_bytes)
            except ValueError as exc:
                raise SnapshotIntegrityError('snapshot_metadata_unverified') from exc
            self.metadata_bytes += len(payload)
            check_budget()
            return payload
        # Resolve once: no raw-byte cache and no second decoded snapshot.
        self.resolved = resolve_snapshot(manifest, load)
        self.partitions = self.resolved['selected_partitions']
        self.entries = sorted(manifest['files'] + [f for p in self.partitions for f in p['files']],
                              key=lambda f: f['path'])
        self.shards = [dict(path=r['path'], sha256=r['sha256'], rows=0)
                       for r in manifest['coverage_shards']]

    def verify(self, entries=None):
        verify_files(self.root, self.entries + self.shards if entries is None else entries,
                     self.manifest['data_schema_version'], self.partitions)

    def select(self, filters):
        return [p for p in self.partitions if overlaps(p, filters)]

    def dataset(self, partitions):
        paths = [str(self.root / f['path']) for p in partitions for f in p['files']]
        return ds.dataset(paths, schema=schema_for(self.manifest['data_schema_version']), format='parquet')

    def coverage(self, filters):
        cov = {name: [p for p in self.resolved['coverage'][name] if overlaps(p, filters)]
               for name in sorted(COVERAGE)}
        cov.update(complete=not cov['not_collected'], scope='exact-service-region-month-sets',
                   gaps_overlapping_query=cov['not_collected'],
                   stale={'status': 'unknown', 'reason': 'No freshness SLA is asserted; inspect fetched_at.'})
        return cov

    def provenance(self, partitions):
        return [{name: p[name] for name in sorted(KEY | {
            'producer_release_id', 'origin_digest', 'observation_id', 'run_id',
            'fetched_at', 'status', 'evidence_digest', 'previous_observation'})} for p in partitions]
