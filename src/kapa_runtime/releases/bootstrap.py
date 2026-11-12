# SPDX-License-Identifier: GPL-3.0-only
"""Explicit, evidence-bound legacy imports; never discover coverage from Hub files."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
import tempfile
from pathlib import Path

from .. import pipeline as p, accumulation as a
from ..storage.metadata import read_json
from ..storage.state import StateStore, _dump
from .activate import _atomic_write
from .manifest import build_manifest, dumps_manifest, manifest_digest, validate_manifest
from .manifest_v2 import coverage_for_partitions, key
from .snapshot import check_parent, materialize, published_digest


def legacy_partitions(settings, store, release_id):
    """Bind local replay evidence to the published inventory and original ledger."""
    p._id(release_id)
    row = store.get_release(release_id)
    p._require(row is None or row['state'] in ('published', 'active'),
               'legacy_serving_state_required')
    try:
        published_digest(store, release_id)
    except ValueError:
        raise p.PipelineError('legacy_digest_required_run_attest_legacy') from None
    origin = p._trusted_published_manifest(settings, store, release_id)
    p._require(origin['schema_version'] == 'kapa-runtime-release/1'
               and origin['data_schema_version'] == 'kapa-runtime-data/2',
               'legacy_metadata_unverifiable_reobserve')
    root = settings.snapshots_dir / release_id
    p._verify_local(root, origin)
    try:
        meta = read_json(p._metadata(settings, release_id))
        cov = origin['coverage']
        p._require(meta['release_id'] == release_id and cov['complete'] is True
                   and set(cov['services']) == set(p.SERVICES)
                   and all(meta[k] == cov[k] for k in ('lawd_code', 'deal_ymd')),
                   'legacy_metadata_unverifiable_reobserve')
        files = p._verify_complete(p._root(settings, release_id), meta)
        p._require(sorted(files, key=lambda f: f['path']) ==
                   sorted((f for f in origin['files'] if f['path'].startswith('data/')),
                          key=lambda f: f['path']), 'legacy_metadata_unverifiable_reobserve')
        parts = []
        for service, completed in meta['completed'].items():
            row = store._connection.execute('SELECT data FROM observations WHERE observation_id=?',
                                            (completed['observation_id'],)).fetchone()
            p._require(row is not None, 'legacy_metadata_unverifiable_reobserve')
            obs = json.loads(row[0])
            raw = p._root(settings, release_id) / 'raw' / service / completed['observation_id']
            pages = sorted(raw.glob('page-*.xml'))
            payload_hash = hashlib.sha256()
            for page in pages:
                p._safe_path(page)
                payload_hash.update(page.read_bytes())
            p._require(payload_hash.hexdigest() == obs['sanitized_payload_sha256'],
                       'legacy_metadata_unverifiable_reobserve')
            p._require(all(obs[k] == completed[k] for k in ('run_id', 'observation_id',
                       'status', 'pages_expected', 'pages_received'))
                       and datetime.fromisoformat(obs['fetched_at']) == datetime.fromisoformat(completed['fetched_at'])
                       and obs['count'] == completed['rows'] and obs['coverage']['complete'] is True
                       and obs['service_id'] == service
                       and obs['request']['LAWD_CD'] == meta['lawd_code']
                       and obs['request']['DEAL_YMD'] == meta['deal_ymd']
                       and obs['run_id'] in origin['source_runs']
                       and obs['parser_version'] == origin['parser_version'],
                       'legacy_metadata_unverifiable_reobserve')
            parts.append(dict(service_id=service, lawd_code=meta['lawd_code'], deal_ymd=meta['deal_ymd'],
                producer_release_id=release_id, origin_digest=manifest_digest(origin),
                **{k: completed[k] for k in ('observation_id', 'run_id', 'fetched_at', 'status', 'rows', 'files')},
                parser_version=obs['parser_version'], schema=origin['data_schema_version'],
                previous_observation=None, evidence_digest=a.digest(dict(observation=obs, completed=completed))))
        return origin, sorted(parts, key=key)
    except (KeyError, TypeError, ValueError, OSError):
        raise p.PipelineError('legacy_metadata_unverifiable_reobserve') from None


def verify_imports(settings, store, manifest, imports):
    """Recheck all origins on publication/retry, not just when freezing the job."""
    expected = []
    for rid in imports:
        _, parts = legacy_partitions(settings, store, rid)
        expected.extend(parts)
    p._require(sorted(expected, key=key) == manifest['selected_partitions'], 'bootstrap_evidence_changed')


def _prepare_snapshot(settings, store, release_id, data):
    """Resume frozen preparation; abandoned random temp directories are never reused.

    Interrupted temp directories may be removed by an operator with writers stopped.
    Only a verified, atomically renamed directory becomes the snapshot.
    """
    m = validate_manifest(data['manifest'])
    p._require(manifest_digest(m) == data['snapshot_digest']
               and a.digest(dict(manifest=m, card=data['card'], imports=data['imports']))
               == data['inputs_digest'], 'bootstrap_inputs_changed')
    check_parent(store, m['parent_snapshot'], active_path=settings.active_manifest)
    verify_imports(settings, store, m, data['imports'])
    root = settings.snapshots_dir / release_id
    p._safe_path(root)
    if root.exists():
        p._verify_local(root, m)
    else:
        settings.snapshots_dir.mkdir(parents=True, exist_ok=True)
        # Unique attempts safely ignore crash leftovers, including partial bytes.
        with tempfile.TemporaryDirectory(dir=settings.snapshots_dir,
                                         prefix='bootstrap-preparing-') as directory:
            temporary = Path(directory) / release_id
            sources = {entry['path']: settings.snapshots_dir / part['producer_release_id']
                       for part in m['selected_partitions'] for entry in part['files']}
            def load(name):
                return data['card'].encode() if name == 'README.md' else a._load(sources[name], name)
            materialize(m, temporary, load, enabled=True)
            temporary.rename(root)
    _atomic_write(settings.manifests_dir / (release_id + '.json'), dumps_manifest(m).encode())
    store._connection.execute("UPDATE collection_jobs SET state='built' WHERE job_id=?", (release_id,))


def bootstrap_cumulative(settings, *, release_id, import_release=None, dry_run=False, api=None):
    from .batch import publish_batch
    invoked_at = datetime.now(timezone.utc)
    p._id(release_id)
    imports = sorted(set(import_release or []))
    for rid in imports:
        p._id(rid)
    with StateStore(settings.state_db) as store:
        store.migrate_jobs()
        existing = store._connection.execute('SELECT data,state FROM collection_jobs WHERE job_id=?',
                                            (release_id,)).fetchone()
        if existing:
            data = json.loads(existing[0])
            p._require(data.get('kind') == 'bootstrap' and data['explicit_imports'] == imports,
                       'bootstrap_job_conflict')
            prepared = None
            if existing[1] == 'preparing':
                p._require(not dry_run, 'bootstrap_preparation_resume_required')
                _prepare_snapshot(settings, store, release_id, data)
        else:
            parent = p._read_manifest(settings.active_manifest)
            p._require(parent['schema_version'] == 'kapa-runtime-release/1', 'legacy_active_required')
            # Produce actionable missing-attestation guidance before check_parent.
            legacy_partitions(settings, store, parent['release_id'])
            check_parent(store, dict(release_id=parent['release_id'], digest=manifest_digest(parent)),
                         active_path=settings.active_manifest)
            p._require(store.get_release(parent['release_id'])['state'] == 'active', 'legacy_active_required')
            p._require(not store._connection.execute(
                "SELECT 1 FROM collection_jobs WHERE state IN ('preparing','running','paused','built','blocked')").fetchone(),
                'resume_existing_job_required')
            a._new_job_id(settings, store, parent, release_id)
            origins = sorted(set([parent['release_id'], *imports]))
            parts, sources = [], {}
            for rid in origins:
                origin, selected = legacy_partitions(settings, store, rid)
                p._require(origin['hugging_face_revisions'][0]['repo_id'] ==
                           parent['hugging_face_revisions'][0]['repo_id'], 'legacy_target_mismatch')
                parts.extend(selected)
                for part in selected:
                    for entry in part['files']:
                        p._require(entry['path'] not in sources, 'legacy_import_overlap')
                        sources[entry['path']] = settings.snapshots_dir / rid
            p._require(len({key(part) for part in parts}) == len(parts), 'legacy_import_overlap')
            parts.sort(key=key)
            pairs = [dict(lawd_code=c, deal_ymd=m) for c, m in sorted({key(part)[:2] for part in parts})]
            p._require(1 <= len(pairs) <= 60, 'batch_cap_exceeded')
            policy = p._policy(settings)
            sha = p._git_revision(settings)
            p._require(policy.get('fixture_evidence', {}).get('passed') is True and
                       policy['fixture_evidence'].get('github_revision') == sha, 'build_evidence_required')
            siblings, warnings = p.workspace_guard.capture(settings)
            m = build_manifest(release_id=release_id, state='validated', files=[],
                created_at=invoked_at, parser_version=parent['parser_version'],
                source_runs=sorted({part['run_id'] for part in parts}),
                github_revisions={'pacappkor/kapa_runtime': sha, **siblings},
                quality=dict(passed=True, legal_verified=False, fixture_revision=sha,
                             terms_evidence=a._terms(policy), workspace_warnings=warnings))
            m.update(schema_version='kapa-runtime-release/2', release_kind='bootstrap',
                parent_snapshot=dict(release_id=parent['release_id'], digest=manifest_digest(parent)),
                batch_scope=dict(declared_pairs=pairs), selected_partitions=parts, coverage_shards=[],
                coverage=coverage_for_partitions(parts, [{k: part[k] for k in
                    ('lawd_code', 'deal_ymd', 'service_id')} for part in parts]))
            # No wildcard: the default viewer selects only the proven import inventory.
            card = ('---\nconfigs:\n- config_name: default\n  data_files:\n  - split: train\n    path:\n'
                    + ''.join('    - ' + json.dumps(name) + '\n' for name in sorted(sources))
                    + '---\n# Cumulative bootstrap\n\nOriginal observations, not new collection. '
                    'Partial addresses; legal_verified=false. See manifest for exact coverage.\n').encode()
            m['files'] = [dict(path='README.md', sha256=hashlib.sha256(card).hexdigest(), rows=0)]
            m = validate_manifest(m)
            data = dict(kind='bootstrap', explicit_imports=imports, imports=origins,
                        plan=dict(items=pairs), snapshot_digest=manifest_digest(m))
            data['plan_digest'] = a.digest(data['plan'])
            def load(name):
                return card if name == 'README.md' else a._load(sources[name], name)
            if dry_run:
                # No job, manifest, snapshot ledger, or publication journal is created.
                settings.cache_dir.mkdir(parents=True, exist_ok=True)
                temporary = tempfile.TemporaryDirectory(dir=settings.cache_dir, prefix='bootstrap-preview-')
                root = Path(temporary.name) / release_id
                materialize(m, root, load, enabled=True)
                prepared = (m, root, data)
            else:
                data.update(manifest=m, card=card.decode(), created_at=m['created_at'])
                data['inputs_digest'] = a.digest(dict(manifest=m, card=data['card'], imports=origins))
                # SQLite autocommit makes the frozen intent durable before any mkdir.
                store._connection.execute('INSERT INTO collection_jobs VALUES (?,?,?)',
                                          (release_id, 'preparing', _dump(data)))
                _prepare_snapshot(settings, store, release_id, data)
                prepared = None
    try:
        result = publish_batch(settings, job_id=release_id, dry_run=dry_run, api=api,
                               _preview=prepared if dry_run else None)
        if dry_run:
            m = prepared[0] if prepared else p._read_manifest(settings.manifests_dir / (release_id + '.json'))
            result.update(release_id=release_id, release_kind='bootstrap', parent=m['parent_snapshot'],
                partitions=len(m['selected_partitions']), manifest_digest=manifest_digest(m),
                backup='Stop writers; back up the entire data_root including ledger, manifests, snapshots, staging and publication-spool before bootstrap.')
        return result
    finally:
        if dry_run and prepared:
            temporary.cleanup()
