# SPDX-License-Identifier: GPL-3.0-only
"""Fail-closed daily wrapper. A quota pause preserves the same frozen job."""
from datetime import datetime
import os
from zoneinfo import ZoneInfo

from . import accumulation, pipeline as p
from .releases.activate import _atomic_write
from .releases.batch import publish_batch
from .storage.parquet import _safe_path


def clear_stop(settings):
    path = settings.data_root / 'daily' / 'STOP'
    _safe_path(path)
    path.unlink(missing_ok=True)
    return {'state': 'cleared', 'warning': 'pending_job_and_ledger_blocks_unchanged'}


def daily_run(settings, *, fixture_revision=None, clock=lambda: datetime.now(ZoneInfo('Asia/Seoul'))):
    from .state_bundle import exclusive
    try:
        with exclusive(settings):
            return _daily_run(settings, fixture_revision=fixture_revision, clock=clock)
    except FileExistsError:
        return {'state': 'locked', 'exit_code': 4}


def _daily_run(settings, *, fixture_revision=None, clock):
    root = settings.data_root / 'daily'
    _safe_path(root)
    root.mkdir(parents=True, exist_ok=True)
    stop, pending, lock = (root / name for name in ('STOP', 'pending-job.txt', 'lock'))
    for path in (stop, pending, lock):
        _safe_path(path)
    if stop.exists():
        return {'state': 'stopped', 'exit_code': 4}
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return {'state': 'locked', 'exit_code': 4}
    try:
        try:
            if fixture_revision is not None:
                p._require(fixture_revision == p._git_revision(settings), 'fixture_revision_mismatch')
                policy = p._policy(settings)
                evidence = {'passed': True, 'github_revision': fixture_revision}
                if pending.exists():
                    p._require(policy.get('fixture_evidence') == evidence, 'pending_fixture_revision_changed')
                else:
                    policy['fixture_evidence'] = evidence
                    p._json_write(settings.data_root / 'operator-policy.json', policy)
            if pending.exists():
                job = p._id(pending.read_text(encoding='utf-8').strip())
            else:
                # Planning is coverage-aware and retains refresh/backfill/audit lanes.
                accumulation.plan(settings, clock=clock)
                job = 'daily-' + clock().strftime('%Y%m%d-%H%M%S')
                _atomic_write(pending, job.encode())
            from .storage.state import StateStore
            with StateStore(settings.state_db) as store:
                release = store.get_release(job)
            already_published = release and release['state'] in ('published', 'active')
            result = ({'state': 'validated'} if already_published else
                      accumulation.collect_batch(settings, job_id=job, clock=clock))
            if result.get('state') == 'paused':
                return {'state': 'paused', 'job_id': job, 'exit_code': 0}
            if result.get('state') == 'empty':
                pending.unlink()
                return {'state': 'empty', 'exit_code': 0}
            p._require(result.get('state') == 'validated', 'daily_collection_failed')
            result = publish_batch(settings, job_id=job)
            p._require(result.get('state') == 'published', 'daily_publication_failed')
            result = p.activate(settings, release_id=job, cumulative=True)
            p._require(result.get('state') == 'active', 'daily_activation_failed')
            result = p.query(settings, filters={'lawd_code': '11110'}, limit=1)
            p._require(result.get('snapshot_id') == job, 'daily_query_mismatch')
            pending.unlink()
            return {'state': 'active', 'job_id': job, 'exit_code': 0}
        except Exception:
            # Never persist arbitrary upstream exception text or credentials.
            _atomic_write(stop, b'daily_run_failed; operator investigation required\n')
            return {'state': 'failed', 'error_code': 'daily_run_failed', 'exit_code': 1}
    finally:
        os.close(fd)
        lock.unlink()
