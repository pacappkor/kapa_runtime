# SPDX-License-Identifier: GPL-3.0-only
"""Opt-in cumulative assembly. Trust is supplied by the publication ledger, not hashes alone."""
from __future__ import annotations

import copy
import hashlib
import os
import json

from . import manifest_v2 as v2
from .manifest import _json
from pathlib import Path

from .manifest import manifest_digest, validate_manifest, relative_path
from .manifest_v2 import (KEY, assemble_partitions, coverage_for_partitions, key,
                          resolve_snapshot, resolve_snapshot_inventory, require)
from ..storage.parquet import _safe_path


def _unique_evidence(values, identity, message):
    """Collapse only identical evidence, before any normalization can hide conflicts."""
    indexed = {}
    for value in values:
        name = identity(value)
        require(name not in indexed or indexed[name] == value, message)
        indexed[name] = value
    return indexed


def published_digest(store, release_id):
    row = store.get_release(release_id)
    evidence = [e['evidence'] for e in row['guard_evidence']
                if e['state'] == 'published' and e['evidence'].get('commit_reread') is True] if row else []
    require(len(evidence) == 1, 'Publication evidence required')
    value = evidence[0].get('published_manifest_sha256')
    require(isinstance(value, str) and len(value) == 64, 'Publication digest required')
    return value


def check_parent(store, parent, *, active_path, publication_job=None):
    """Read the current pointer under the writer lock, never a caller's cached manifest."""
    from .activate import load_manifest
    require(store._lock_fd is not None, 'StateStore writer lock required')
    active_path = Path(active_path)
    _safe_path(active_path)
    active = load_manifest(active_path) if active_path.exists() else None
    require(active is not None and parent == {'release_id': active['release_id'],
            'digest': manifest_digest(active)}, 'Parent changed')
    require(published_digest(store, active['release_id']) == parent['digest'], 'Parent untrusted')
    require(not any(r['state'] == 'published' for r in store.list_releases()),
            'Published-but-not-active release exists')
    require(not store.pending_promotions(), 'Unresolved publication journal')
    if getattr(store, '_connection', None) is not None and store._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='batch_publications'").fetchone():
        pending = {r[0] for r in store._connection.execute('SELECT job_id FROM batch_publications')}
        require(not pending - {publication_job}, 'Unresolved publication journal')


def _owned_pairs(block):
    return {key(p)[:2] for p in block['selected_partitions']} | {
        key(p)[:2] for name in v2.COVERAGE for p in block['coverage'][name]}


def check_pair_ownership(result, loader):
    """Reject overlapping root/shard ownership, including gap-only pairs."""
    owned = _owned_pairs(result)
    for ref in result.get('coverage_shards', []):
        pairs = _owned_pairs(json.loads(loader(ref['path'])))
        require(owned.isdisjoint(pairs), 'Pair must belong to a unique block')
        owned.update(pairs)


def preflight_selection(parent, resolved, items, loader):
    """Exercise the same block layout before collecting any replacement bytes."""
    result = copy.deepcopy(resolved)
    declared = {key(p): {k: p[k] for k in KEY}
                for p in result['coverage']['declared'] + result['selected_partitions']}
    for item in items:
        for service in v2.SERVICES:
            entry = dict(lawd_code=item['lawd_code'], deal_ymd=item['deal_ymd'], service_id=service)
            declared[key(entry)] = entry
    result['coverage']['declared'] = list(declared.values())
    result['batch_scope'] = {'declared_pairs': items}
    _sharded_selection(result, parent, loader, {})


def _sharded_selection(result, original, loader, shard_payloads):
    """Keep untouched blocks byte-identical; split changed blocks on pair boundaries."""
    selected = {key(p): p for p in result['selected_partitions']}
    declared = {key(p): p for p in result['coverage']['declared']}
    touched = {(p['lawd_code'], p['deal_ymd']) for p in result['batch_scope']['declared_pairs']}
    refs = []
    blocks = []
    for ref in original.get('coverage_shards', []):
        block = json.loads(loader(ref['path']))
        pairs = _owned_pairs(block)
        if pairs.isdisjoint(touched):
            refs.append(ref)
            selected = {k: p for k, p in selected.items() if k[:2] not in pairs}
            declared = {k: p for k, p in declared.items() if k[:2] not in pairs}
        else:
            blocks.append(pairs)
    blocks.append({k[:2] for k in declared} - set().union(set(), *blocks))
    for pairs in blocks:
        # At most 8 entries per pair (two selected and six coverage entries).
        chunk = []
        def emit(group):
            if not group:
                return
            ps = [selected[k] for k in sorted(selected) if k[:2] in group]
            ds = [declared[k] for k in sorted(declared) if k[:2] in group]
            cov = coverage_for_partitions(ps, ds)
            block = dict(schema_version='kapa-runtime-coverage/1', selected_partitions=ps,
                         coverage=cov, license='SPDX-License-Identifier: GPL-3.0-only')
            payload = (_json(block) + '\n').encode()
            count = len(ps) + sum(len(cov[n]) for n in v2.COVERAGE)
            if len(payload) > v2.MAX_METADATA_BYTES or count > v2.MAX_SHARD_ENTRIES:
                require(len(group) > 1, 'Metadata limit exceeded')
                middle = len(group) // 2
                emit(group[:middle])
                emit(group[middle:])
                return
            sha = hashlib.sha256(payload).hexdigest()
            name = f'manifests/coverage/{sha}.json'
            shard_payloads[name] = payload
            refs.append(dict(path=name, sha256=sha, entries=count))
        for pair in sorted(pairs):
            chunk.append(pair)
            if len(chunk) >= max(1, v2.MAX_SHARD_ENTRIES // 8):
                emit(chunk)
                chunk = []
        emit(chunk)
    result.update(selected_partitions=[], coverage=coverage_for_partitions([], []),
                  coverage_shards=sorted(refs, key=lambda r: r['path']))
    check_pair_ownership(result, lambda name: shard_payloads[name] if name in shard_payloads else loader(name))


def assemble_snapshot(template, parent, batch, *, trusted_digests, origins,
                      loader=None, legacy_partitions=None, tombstones=(), retained=(),
                      shard_payloads=None, enabled=False):
    """Pure assembly with explicit trusted publications and origin manifests.

    A v1 parent needs externally verified legacy partition evidence; this function
    never invents missing run/provenance fields. ``trusted_digests`` must come from
    published ledger events (use build_snapshot for the stateful boundary).
    """
    require(enabled is True, 'Cumulative snapshots disabled')
    parent = resolve_snapshot(parent, loader)
    require(parent['data_schema_version'] == 'kapa-runtime-data/2', 'Mixed snapshot schema')
    parent_digest = trusted_digests.get(parent['release_id'])
    # The root digest, not the expanded shard view, is the trust anchor.
    original = origins.get(parent['release_id'])
    require(original is not None and manifest_digest(original) == parent_digest, 'Parent untrusted')
    require(resolve_snapshot(original, loader) == parent, 'Parent evidence mismatch')
    inherited = copy.deepcopy(parent.get('selected_partitions', legacy_partitions))
    require(isinstance(inherited, list), 'Legacy partition evidence required')
    inherited = list(_unique_evidence(inherited, key, 'Conflicting inherited partition evidence').values())
    batch = list(_unique_evidence(batch, key, 'Conflicting batch partition evidence').values())
    if parent['schema_version'] == 'kapa-runtime-release/1':
        require(sorted([f for p in inherited for f in p['files']], key=lambda f: f['path']) ==
                sorted([f for f in parent['files'] if f['path'].startswith('data/')], key=lambda f: f['path']),
                'Legacy inventory mismatch')
    for p in inherited:
        producer = p['producer_release_id']
        origin = origins.get(producer)
        bound = trusted_digests.get(producer)
        require(origin is not None and manifest_digest(origin) == bound, 'Origin untrusted')
        require(p['origin_digest'] in (None, bound) and
                (p['origin_digest'] is not None or producer == parent['release_id']), 'Origin binding mismatch')
        resolved = resolve_snapshot(origin, loader)
        if resolved['schema_version'] == 'kapa-runtime-release/2':
            expected = next((v for v in resolved['selected_partitions'] if key(v) == key(p)), None)
            require(expected is not None and {**expected, 'origin_digest': bound} ==
                    {**p, 'origin_digest': bound}, 'Origin partition mismatch')
        else:
            require(all(f in resolved['files'] for f in p['files']), 'Legacy origin inventory mismatch')
            require(resolved['data_schema_version'] == p['schema'], 'Legacy origin schema mismatch')
        p['origin_digest'] = bound
    batch = copy.deepcopy(batch)
    old = {key(p): p for p in inherited}
    for p in batch:
        require(p['producer_release_id'] == template['release_id'] and p['origin_digest'] is None,
                'New producer mismatch')
        if key(p) in old:
            previous = old[key(p)]
            p['previous_observation'] = dict(observation_id=previous['observation_id'],
                producer_release_id=previous['producer_release_id'], digest=previous['origin_digest'])
    selected = assemble_partitions(inherited, batch)
    physical = _unique_evidence(retained, lambda f: f['path'], 'Conflicting retained path evidence')
    for p in inherited + batch:
        for f in p['files']:
            require(f['path'] not in physical or physical[f['path']] == f, 'Immutable path conflict')
            physical[f['path']] = f
    require(not set(tombstones).intersection(f['path'] for p in selected for f in p['files']),
            'Tombstone resurrection refused')
    declared = {key(v): {k: v[k] for k in KEY} for v in parent.get('coverage', {}).get('declared', [])}
    declared.update({key(p): {k: p[k] for k in KEY} for p in selected})
    result = copy.deepcopy(template)
    result.update(schema_version='kapa-runtime-release/2', release_kind='batch',
        parent_snapshot=dict(release_id=parent['release_id'], digest=parent_digest),
        selected_partitions=selected, coverage_shards=[],
        batch_scope={'declared_pairs': [dict(lawd_code=c, deal_ymd=m)
                     for c, m in sorted({key(p)[:2] for p in batch})]},
        coverage=coverage_for_partitions(selected, [declared[k] for k in sorted(declared)]))
    if original.get('coverage_shards') or len((_json(result) + '\n').encode()) > v2.MAX_METADATA_BYTES:
        require(shard_payloads is not None, 'Shard output required')
        result['quality']['inherited_origins'] = {
            **original.get('quality', {}).get('inherited_origins', {}),
            parent['release_id']: parent_digest}
        _sharded_selection(result, original, loader, shard_payloads)
    checked = validate_manifest(result)
    resolve_snapshot(checked, lambda name: shard_payloads[name] if name in (shard_payloads or {}) else loader(name))
    return checked


def build_snapshot(store, template, parent, batch, *, active_path, origins, enabled=False, **kwargs):
    require(enabled is True, 'Cumulative snapshots disabled')
    check_parent(store, dict(release_id=parent['release_id'], digest=manifest_digest(parent)),
                 active_path=active_path)
    trusted = {rid: published_digest(store, rid) for rid in origins}
    result = assemble_snapshot(template, parent, batch, trusted_digests=trusted,
                               origins=origins, enabled=True, **kwargs)
    payloads = kwargs.get('shard_payloads') or {}
    store.record_snapshot(result, loader=lambda name: payloads[name] if name in payloads else kwargs['loader'](name))
    return result


def write_once(root, name, payload, digest):
    """Create immutable bytes exclusively; an existing identical file is reusable."""
    require(hashlib.sha256(payload).hexdigest() == digest, 'Payload digest mismatch')
    name = relative_path(name)
    require(name.startswith('data/observations/'), 'Immutable observation path required')
    path = Path(root) / name
    _safe_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open('xb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        require(hashlib.sha256(path.read_bytes()).hexdigest() == digest, 'Immutable path conflict')
    return path


def materialize(manifest, root, loader, *, enabled=False):
    """Install a complete selected inventory into a new directory, then verify it.

    The loader is local or revision-pinned by the caller. No pointer is changed.
    A failed attempt leaves an unusable directory, never a partially active one.
    """
    require(enabled is True, 'Cumulative snapshots disabled')
    from .activate import _atomic_write
    from ..pipeline import _verify_local
    root = Path(root)
    _safe_path(root)
    entries = resolve_snapshot_inventory(manifest, loader)
    root.mkdir(parents=True, exist_ok=False)
    entries += [dict(path=r['path'], sha256=r['sha256'], rows=0)
                for r in manifest.get('coverage_shards', [])]
    for entry in entries:
        payload = loader(entry['path'])
        require(hashlib.sha256(payload).hexdigest() == entry['sha256'], 'Materialization digest mismatch')
        path = root / relative_path(entry['path'])
        _safe_path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, payload)
    _verify_local(root, manifest)
    return root


def inventory(manifest, root):
    """Resolve explicit inventory with safely bounded local shard reads."""
    from ..storage.metadata import read_metadata
    if manifest.get('schema_version') != 'kapa-runtime-release/2':
        return manifest['files']
    def load(name):
        path = Path(root) / relative_path(name)
        _safe_path(path)
        return read_metadata(path, as_bytes=True)
    return resolve_snapshot_inventory(manifest, load)
