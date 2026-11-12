# SPDX-License-Identifier: GPL-3.0-only
"""Frozen v2 job publication through private verification and journaled deltas."""
from __future__ import annotations

import copy
import hashlib
import json
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from .hf import PublishError, _verified_promotion
from .manifest import dumps_manifest, manifest_digest, relative_path
from .manifest_v2 import resolve_snapshot, resolve_snapshot_inventory
from .snapshot import check_parent
from .spool import Spool, inventory_digest
from ..storage.state import StateStore, REQUIRED_GUARDS, _dump
from ..models import ReleaseState


def _save(store, job, data):
    # Manifests contain reviewed documentation URLs, not ledger-safe metadata.
    # Freeze the complete journal atomically before replacing its ledger pointer.
    descriptor = Spool(store.path.parent / 'publication-spool').put_document(data)
    body = _dump({'document_blob': descriptor})
    store._connection.execute('INSERT OR REPLACE INTO batch_publications VALUES (?,?,?)',
                              (job, body, hashlib.sha256(body.encode()).hexdigest()))


def _load(store, job):
    row = store._connection.execute('SELECT data,digest FROM batch_publications WHERE job_id=?', (job,)).fetchone()
    if row is None:
        return None
    if hashlib.sha256(row[0].encode()).hexdigest() != row[1]:
        raise PublishError('Publication journal corrupt')
    data = json.loads(row[0])
    if 'document_blob' in data:
        return Spool(store.path.parent / 'publication-spool').read_document(data['document_blob'])
    return data  # Pre-spool, URL-free journals remain resumable.


def _retry_after(exc):
    value = getattr(getattr(exc, 'response', None), 'headers', {}).get('Retry-After', '1')
    try:
        delay = float(value)
    except (ValueError, TypeError):
        try:
            delay = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
        except Exception:
            raise PublishError('Rate limit retry refused', reason='hub_rate_limited') from None
    # Never retry earlier than requested. Long/invalid waits stop instead.
    if not 0 <= delay <= 60:
        raise PublishError('Rate limit retry refused', reason='hub_rate_limited')
    return delay


def _commit(store, job, journal, phase, writer, payloads, parent, cache, guard, *, intent=None,
            sleep=time.sleep):
    """Resolve ambiguous results by full inventory, never by re-sending blindly."""
    record = journal[phase]
    for attempt in range(3):
        head, _ = writer._head_tree()
        if head != parent:
            if not record.get('attempted'):
                raise PublishError('Default-branch head changed before commit')
            if record.get('sha') is not None and record['sha'] != head:
                raise PublishError('Journaled commit head changed')
            # A lost response is accepted only for the exact frozen full tree.
            writer._verify_inventory(head, payloads, cache)
            writer._require_head(head)
            record['sha'] = head
            _save(store, job, journal)
            return head
        if record.get('sha') is not None:
            raise PublishError('Journaled commit head changed')
        def before_commit():
            guard()
            writer._require_head(parent)
            record['attempted'] = True
            _save(store, job, journal)
        try:
            sha = writer._replace_files(payloads, expected_parent=parent, evidence_store=store,
                cache_dir=cache, _promotion=intent, delta=True,
                tombstones=record['tombstones'], before_commit=before_commit)
        except Exception as exc:
            head, _ = writer._head_tree()
            if head != parent:
                if not record.get('attempted'):
                    raise PublishError('Default-branch head changed before commit') from None
                writer._verify_inventory(head, payloads, cache)
                writer._require_head(head)
                record['sha'] = head
                _save(store, job, journal)
                return head
            if getattr(getattr(exc, 'response', None), 'status_code', None) != 429:
                raise
            if attempt == 2:
                raise PublishError('Rate limit exhausted', reason='hub_rate_limited') from None
            sleep(_retry_after(exc))
            continue
        record['sha'] = sha
        _save(store, job, journal)
        writer._require_head(sha)
        return sha
    raise PublishError('Commit unresolved')


def _entries(manifest, root):
    entries = resolve_snapshot_inventory(manifest, lambda name: (root / relative_path(name)).read_bytes())
    return entries + [dict(path=r['path'], sha256=r['sha256'], rows=0)
                      for r in manifest.get('coverage_shards', [])]


def _envelope(manifest, repo, sha, candidate_hash, staging=None):
    result = copy.deepcopy(manifest)
    result['files'].append(dict(path='manifests/release.json', sha256=candidate_hash, rows=0))
    result['files'].sort(key=lambda f: f['path'])
    result.update(state='published', hugging_face_revisions=[dict(repo_id=repo, commit_sha=sha)])
    if staging:
        result['staging_revision'] = staging
    return result


def publish_batch(settings, *, job_id, dry_run=False, api=None, sleep=time.sleep, _preview=None):
    from .. import pipeline as p, accumulation as a
    from .activate import _atomic_write
    p._id(job_id)
    p._require(settings.hf_namespace and settings.hf_repos.get('market'), 'hf_target_required')
    p._require(settings.hf_visibilities.get('market') == 'public', 'batch_requires_public_market')
    with StateStore(settings.state_db) as store:
        store.migrate_jobs()
        store._connection.execute('CREATE TABLE IF NOT EXISTS batch_publications '
                                  '(job_id TEXT PRIMARY KEY,data TEXT NOT NULL,digest TEXT NOT NULL)')
        if store.pending_promotions():
            p._require(not dry_run, 'recovery_required')
            p._restore_public(settings, store, job_id, api)
        pending = [r[0] for r in store._connection.execute('SELECT job_id FROM batch_publications')]
        p._require(not set(pending) - {job_id} and not store.pending_promotions(), 'unresolved_publication')
        job = store._connection.execute('SELECT state,data FROM collection_jobs WHERE job_id=?', (job_id,)).fetchone()
        if _preview is not None:
            p._require(dry_run and job is None, 'invalid_publication_preview')
            job = ('built', _dump(_preview[2]))
        p._require(job is not None and job[0] in ('built', 'published'), 'built_job_required')
        data = json.loads(job[1])
        p._require(a.digest(data['plan']) == data['plan_digest'], 'frozen_plan_changed')
        journal = _load(store, job_id)
        release = store.get_release(job_id)
        if journal and release and release['state'] == 'published':
            final = p._trusted_published_manifest(settings, store, job_id)
            p._require(final == journal.get('final_manifest'), 'publication_ledger_conflict')
            writer = p._publisher(settings, api)
            p._require(not dry_run, 'recovery_required')
            p._require(writer.repo_id == journal['public']['repo'], 'publication_target_changed')
            spool = Spool(settings.state_db.parent / 'publication-spool')
            payloads = spool.replay(journal['inventory'], journal['inventory_digest'], writer)
            sha = journal['public']['sha']
            writer._verify_inventory(sha, payloads, settings.cache_dir / (job_id + '-recovery'))
            writer._require_head(sha)
            store._connection.execute("UPDATE collection_jobs SET state='published' WHERE job_id=?", (job_id,))
            store._connection.execute('DELETE FROM batch_publications WHERE job_id=?', (job_id,))
            return dict(state='published', release_id=job_id, commit_sha=sha)
        if job[0] == 'published':
            p._require(journal is None, 'unresolved_publication')
            m = p._trusted_published_manifest(settings, store, job_id)
            return dict(state='published', release_id=job_id, commit_sha=m['hugging_face_revisions'][0]['commit_sha'])
        m = _preview[0] if _preview else p._read_manifest(settings.manifests_dir / (job_id + '.json'))
        root = _preview[1] if _preview else settings.snapshots_dir / job_id
        p._require(m['schema_version'] == 'kapa-runtime-release/2', 'v2_snapshot_required')
        # A completed remote transaction may need only its ledger finalization.
        frozen = journal['manifest'] if journal else m
        p._require(m == frozen or (journal and m == journal.get('final_manifest')), 'frozen_manifest_changed')
        m = frozen
        p._require(manifest_digest(m) == data.get('snapshot_digest'), 'frozen_manifest_changed')
        pairs = sorted(({k: item[k] for k in ('lawd_code', 'deal_ymd')} for item in data['plan']['items']),
                       key=lambda v: (v['lawd_code'], v['deal_ymd']))
        p._require(m['batch_scope']['declared_pairs'] == pairs, 'frozen_batch_changed')
        p._require(1 <= len(m['batch_scope']['declared_pairs']) <= 60, 'batch_cap_exceeded')
        p._verify_local(root, m)
        if not dry_run:
            store.record_snapshot(m, loader=lambda name: a._load(root, name))
        def guard():
            check_parent(store, m['parent_snapshot'], active_path=settings.active_manifest, publication_job=job_id)
        guard()
        policy = p._policy(settings)
        p._require(a._terms(policy) == m['quality']['terms_evidence'], 'terms_changed_rebuild_required')
        bootstrap = m['release_kind'] == 'bootstrap'
        if bootstrap:
            from .bootstrap import verify_imports
            p._require(data.get('kind') == 'bootstrap', 'bootstrap_evidence_required')
            verify_imports(settings, store, m, data['imports'])
        else:
            p._require(policy.get('accumulation_smoke_verified') is True, 'operator_smoke_required')
        secrets = p._loaded_secrets(settings)
        resolved = resolve_snapshot(m, lambda name: a._load(root, name))
        for pair in ([] if bootstrap else data['plan']['items']):
            completed = a._completed(store, job_id, pair['lawd_code'], pair['deal_ymd'])
            p._require(set(completed) == set(a.SERVICES), 'incomplete_pair')
            for evidence in completed.values():
                a._verify_result(settings, evidence, pair['lawd_code'], pair['deal_ymd'], secrets)
                actual = next(v for v in resolved['selected_partitions']
                              if v['observation_id'] == evidence['partition']['observation_id'])
                p._require({**actual, 'previous_observation': None} == evidence['partition'], 'frozen_batch_changed')
        staging = p._publisher(settings, api, staging=True)
        public = p._publisher(settings, api)
        # Dry-run never creates a repository or sends a commit. Existing targets
        # are required so counts are exact, not estimates against an empty tree.
        if not dry_run:
            staging.ensure_private_repo()
            public.ensure_private_repo()
        spool = Spool(settings.state_db.parent / 'publication-spool')
        cache = settings.cache_dir / (job_id + '-batch')
        if journal is None:
            restore, public_parent = p._public_restore_inventory(settings, store, public)
            retained = p._promotion_retained_files(settings, store, staging.repo_id, public.repo_id)
            payloads = {f['path']: a._load(root, f['path']) for f in _entries(m, root)}
            payloads['manifests/release.json'] = dumps_manifest(m).encode()
            for entry in retained:
                if entry['path'] in payloads:
                    continue
                value = restore.get(entry['path'])
                p._require(value is not None and hashlib.sha256(value).hexdigest() == entry['sha256'],
                           'retained_inventory_unavailable')
                payloads[entry['path']] = value
            if '.gitattributes' in restore:
                payloads['.gitattributes'] = restore['.gitattributes']
            tombstones = set()
            for repo in (staging.repo_id, public.repo_id):
                p._retained_owners(settings, store, repo, tombstones=tombstones)
            p._require(not tombstones & set(payloads), 'tombstone_resurrection_refused')
            staging_parent, _ = staging._head_tree()
            _, stage_counts = staging._delta_plan(payloads, staging_parent, cache, tombstones)
            _, public_counts = public._delta_plan(payloads, public_parent, cache, tombstones)
            if dry_run:
                return dict(state='planned', dry_run=True, pairs=len(data['plan']['items']),
                            staging=stage_counts, public=public_counts)
            entries = spool.freeze(payloads)
            restore_entries = spool.freeze(restore)
            journal = dict(manifest=m, inventory=entries, inventory_digest=inventory_digest(entries),
                restore=restore_entries, restore_digest=inventory_digest(restore_entries), retained=retained,
                staging=dict(repo=staging.repo_id, parent=staging_parent, tombstones=sorted(tombstones)),
                public=dict(repo=public.repo_id, parent=public_parent, tombstones=sorted(tombstones)))
            _save(store, job_id, journal)
        else:
            p._require(not dry_run, 'recovery_required')
            p._require(journal['staging']['repo'] == staging.repo_id and journal['public']['repo'] == public.repo_id,
                       'publication_target_changed')
            payloads = spool.replay(journal['inventory'], journal['inventory_digest'], staging)
        # Persist the verified collection/build guards before exposing any bytes.
        if store.get_release(job_id) is None:
            store.transition_release(job_id, 'draft', {'job_id': job_id})
        prepublication = ['draft', 'collecting', 'collected', 'normalized', 'validated']
        row = store.get_release(job_id)
        p._require(row['state'] in prepublication + ['uploaded-candidate'], 'publication_state_blocked')
        for state in prepublication[1:]:
            row = store.get_release(job_id)
            if row['state'] == 'uploaded-candidate' or prepublication.index(row['state']) >= prepublication.index(state):
                continue
            evidence = {name: True for name in REQUIRED_GUARDS.get(ReleaseState(state), ())}
            evidence.update(job_id=job_id, validated_manifest_sha256=manifest_digest(m))
            store.transition_release(job_id, state, evidence)
        # Replay immediately before any remote write, including first publication.
        payloads = spool.replay(journal['inventory'], journal['inventory_digest'], staging)
        candidate_hash = hashlib.sha256(payloads['manifests/release.json']).hexdigest()
        staging_sha = _commit(store, job_id, journal, 'staging', staging, payloads,
                              journal['staging']['parent'], cache, guard, sleep=sleep)
        staged = _envelope(m, staging.repo_id, staging_sha, candidate_hash)
        report = staging.verify(staging_sha, staged, cache, previous_files=journal['retained'], evidence_store=store)
        if not report.passed:
            store.transition_release(job_id, 'quarantined', {'reason': 'private_staging_verification_failed'})
            store._connection.execute("UPDATE collection_jobs SET state='blocked' WHERE job_id=?", (job_id,))
            raise p.PipelineError('private_staging_verification_failed')
        staging._require_head(staging_sha)
        intent = _verified_promotion(public.repo_id, staging, report, staged, payloads)
        def promotion_guard():
            guard()
            staging._require_head(staging_sha)
        public_sha = _commit(store, job_id, journal, 'public', public, payloads,
                             journal['public']['parent'], cache, promotion_guard, intent=intent, sleep=sleep)
        final = _envelope(m, public.repo_id, public_sha, candidate_hash,
                          dict(repo_id=staging.repo_id, commit_sha=staging_sha))
        report = public.verify(public_sha, final, cache, previous_files=journal['retained'], evidence_store=store)
        if not report.passed:
            if store.get_release(job_id) is None:
                store.transition_release(job_id, 'draft', {'job_id': job_id})
            store.save_promotion(job_id, dict(repo_id=public.repo_id, public_commit_sha=public_sha,
                restore_inventory=journal['restore'], restore_digest=journal['restore_digest']))
            authority = store._restoration_authority(job_id, public.repo_id)
            restoring = p._publisher(settings, api, restoration=authority)
            restore = spool.replay(journal['restore'], journal['restore_digest'], restoring)
            restored_sha = restoring._restore_journal()
            evidence = store.promotion(job_id)
            evidence['public_commit_sha'] = restored_sha
            store.save_promotion(job_id, evidence)
            restoring._verify_inventory(restored_sha, restore, cache)
            restoring._require_head(restored_sha)
            store.transition_release(job_id, 'quarantined', {'reason': 'public_verification_failed_restored'})
            store._connection.execute("UPDATE collection_jobs SET state='blocked' WHERE job_id=?", (job_id,))
            store.finish_promotion(job_id)
            store._connection.execute('DELETE FROM batch_publications WHERE job_id=?', (job_id,))
            raise p.PipelineError('public_verification_failed_restored')
        public._require_head(public_sha)
        journal['final_manifest'] = final
        _save(store, job_id, journal)
        (root / 'manifests').mkdir(parents=True, exist_ok=True)
        _atomic_write(root / 'manifests/release.json', payloads['manifests/release.json'])
        p._verify_local(root, final)
        _atomic_write(settings.manifests_dir / (job_id + '.json'), dumps_manifest(final).encode())
        # All named guards above were exercised before this durable ledger path.
        if store.get_release(job_id) is None:
            store.transition_release(job_id, 'draft', {'job_id': job_id})
        for state in ('collecting', 'collected', 'normalized', 'validated', 'uploaded-candidate', 'published'):
            row = store.get_release(job_id)
            order = ['draft', 'collecting', 'collected', 'normalized', 'validated', 'uploaded-candidate', 'published']
            if order.index(row['state']) >= order.index(state):
                continue
            evidence = {name: True for name in REQUIRED_GUARDS.get(ReleaseState(state), ())}
            evidence['job_id'] = job_id
            if state == 'published':
                evidence['published_manifest_sha256'] = manifest_digest(final)
            store.transition_release(job_id, state, evidence)
        store._connection.execute("UPDATE collection_jobs SET state='published' WHERE job_id=?", (job_id,))
        store._connection.execute('DELETE FROM batch_publications WHERE job_id=?', (job_id,))
        return dict(state='published', release_id=job_id, commit_sha=public_sha)
