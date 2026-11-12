# SPDX-License-Identifier: GPL-3.0-only
"""Metadata resource bounds apply before loading an entire cumulative snapshot."""
import hashlib
import json
import sys

import pytest

from kapa_runtime.releases import manifest_v2 as v2
from kapa_runtime.releases.manifest import ManifestError, dumps_manifest, validate_manifest
from kapa_runtime.storage import snapshot_query
from kapa_runtime.storage.snapshot_query import SnapshotIntegrityError, SnapshotInventory
from test_manifest_v2 import root, partitions, keys


def many_shards(tmp_path, count=32):
    manifest = root()
    manifest['selected_partitions'] = []
    manifest['coverage'] = v2.coverage_for_partitions([], [])
    for i in range(count):
        parts = partitions(region=str(11110 + i))
        if i:
            for part in parts:
                part.update(producer_release_id='parent', origin_digest='c' * 64)
        shard = dict(license='SPDX-License-Identifier: GPL-3.0-only',
                     schema_version='kapa-runtime-coverage/1', selected_partitions=parts,
                     coverage=v2.coverage_for_partitions(parts, keys(parts)))
        payload = json.dumps(shard).encode()
        sha = hashlib.sha256(payload).hexdigest()
        name = f'manifests/coverage/{sha}.json'
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        manifest['coverage_shards'].append(dict(path=name, sha256=sha, entries=6))
    manifest['coverage_shards'].sort(key=lambda r: r['path'])
    return manifest


def test_aggregate_budget_includes_root_and_all_shards(tmp_path):
    manifest = many_shards(tmp_path)
    root_size = len(dumps_manifest(manifest).encode())
    total = root_size + sum((tmp_path / r['path']).stat().st_size for r in manifest['coverage_shards'])
    with pytest.raises(SnapshotIntegrityError):
        SnapshotInventory(tmp_path, manifest, metadata_budget=total - 1)
    inventory = SnapshotInventory(tmp_path, manifest, metadata_budget=total)
    assert inventory.metadata_bytes == total
    with pytest.raises(SnapshotIntegrityError):
        SnapshotInventory(tmp_path, manifest, metadata_budget=total,
                          root_metadata_bytes=root_size + 1)
    with pytest.raises(SnapshotIntegrityError):
        SnapshotInventory(tmp_path, manifest, metadata_budget=root_size - 1)


def test_many_shards_loaded_once_without_retained_payloads(tmp_path, monkeypatch):
    manifest = many_shards(tmp_path)
    original = snapshot_query.read_metadata
    previous = None
    calls = []

    def load(path, **kwargs):
        nonlocal previous
        if previous is not None:
            # Only this instrumentation retains the previous bytes. A cache or
            # resolver local retaining the payload adds another reference.
            assert sys.getrefcount(previous) == 2
        previous = original(path, **kwargs)
        calls.append(path)
        return previous

    monkeypatch.setattr(snapshot_query, 'read_metadata', load)
    inventory = SnapshotInventory(tmp_path, manifest)
    assert len(calls) == len(set(calls)) == 32
    assert len(inventory.partitions) == 64
    assert sys.getrefcount(previous) == 2


@pytest.mark.parametrize('field', ['parser_version', 'source_runs', 'quality', 'github_revisions',
                                   'unresolved_gaps', 'path', 'partition_parser', 'run_id'])
def test_v2_free_form_string_bounds(field):
    manifest = root()
    if field == 'source_runs':
        manifest[field] = ['x' * 257]
    elif field == 'quality':
        manifest[field]['provenance'] = 'x' * 257
    elif field == 'github_revisions':
        manifest[field] = {'a/' + 'x' * 257: 'a' * 40}
    elif field == 'unresolved_gaps':
        manifest[field] = ['x' * 4097]
    elif field == 'path':
        manifest['files'][0]['path'] = 'x' * 4097
    elif field in ('partition_parser', 'run_id'):
        manifest['selected_partitions'][0]['parser_version' if field == 'partition_parser' else field] = 'x' * 257
    else:
        manifest[field] = 'x' * 257
    with pytest.raises(ManifestError, match='length limit'):
        validate_manifest(manifest)


def test_overlong_shard_field_rejected(tmp_path):
    manifest = many_shards(tmp_path, count=1)
    ref = manifest['coverage_shards'][0]
    shard = json.loads((tmp_path / ref['path']).read_bytes())
    shard['selected_partitions'][0]['parser_version'] = 'x' * 257
    payload = json.dumps(shard).encode()
    sha = hashlib.sha256(payload).hexdigest()
    ref.update(sha256=sha, path=f'manifests/coverage/{sha}.json')
    (tmp_path / ref['path']).write_bytes(payload)
    with pytest.raises(ManifestError, match='length limit'):
        SnapshotInventory(tmp_path, manifest)


def test_active_budget_failure_has_integrity_status(tmp_path):
    from test_cumulative_query import active_fixture
    from kapa_runtime.storage.active import ActiveSnapshotError, search_active

    settings, _ = active_fixture(tmp_path)
    with pytest.raises(ActiveSnapshotError, match='^active_snapshot_unverified$'):
        search_active(settings, {}, metadata_budget=1)


def test_step_3b_terms_evidence_and_versions_remain_supported():
    manifest = root()
    manifest['parser_version'] = 'x' * 256
    manifest['quality']['terms_evidence'] = {
        'molit-apartment-sale': {'terms_reviewed': True, 'terms_basis': 'x' * 4096}}
    validate_manifest(manifest)
    manifest['quality']['terms_evidence']['molit-apartment-sale']['terms_basis'] += 'x'
    with pytest.raises(ManifestError, match='length limit'):
        validate_manifest(manifest)
