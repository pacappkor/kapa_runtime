# SPDX-License-Identifier: GPL-3.0-only
"""Credential-free, read-only access to the ledger-bound active snapshot."""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import re
import sqlite3

from ..releases.manifest import load_manifest, manifest_digest
from .parquet import _safe_path
from .query import TransactionQuery, _json
from .snapshot_query import SnapshotInventory, SnapshotIntegrityError, verify_files
from ..releases.manifest_v2 import key


class ActiveSnapshotError(ValueError):
    """Fixed, value-free read failure."""


def verify_local(root, manifest):
    """Check the complete file inventory, bytes, schema and row counts."""
    if manifest['schema_version'] == 'kapa-runtime-release/2':
        SnapshotInventory(root, manifest).verify()
        return
    _safe_path(root)
    version = manifest["data_schema_version"]
    expected = {f["path"] for f in manifest["files"] if f["path"].endswith(".parquet")}
    actual = set()
    for path in root.rglob("*"):
        _safe_path(path)
        if path.is_file() and path.suffix == ".parquet":
            actual.add(path.relative_to(root).as_posix())
    if expected != actual:
        raise ActiveSnapshotError("snapshot_file_set_mismatch")
    verify_files(root, manifest['files'], version)


def search_active(settings, filters, *, limit=100, cursor=None, coverage_only=False):
    """Capture one active pointer; never follow latest or initialize/write a ledger."""
    _safe_path(settings.active_manifest)
    if not settings.active_manifest.exists():
        raise ActiveSnapshotError("no_active_release")
    try:
        manifest = load_manifest(settings.active_manifest)
        release_id = manifest["release_id"]
        if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", release_id)
                or manifest["state"] not in ("published", "active")):
            raise ValueError
        _safe_path(settings.state_db)
        with closing(sqlite3.connect(settings.state_db.as_uri() + "?mode=ro", uri=True)) as con:
            row = con.execute("SELECT guard_evidence FROM releases WHERE release_id=?", (release_id,)).fetchone()
        evidence = [e["evidence"] for e in json.loads(row[0]) if e["state"] == "published"
                    and e["evidence"].get("commit_reread") is True] if row else []
        if len(evidence) != 1 or evidence[0].get("published_manifest_sha256") != manifest_digest(manifest):
            raise ValueError
        root = settings.snapshots_dir / release_id
        verify_local(root, manifest)
    except Exception:
        raise ActiveSnapshotError("active_snapshot_unverified") from None
    try:
        query = TransactionQuery(root, data_schema_version=manifest['data_schema_version'],
            manifest=manifest if manifest['schema_version'] == 'kapa-runtime-release/2' else None)
        query.root_manifest_digest = manifest_digest(manifest)
        if coverage_only:
            if not isinstance(filters, dict) or filters.keys() - {'service_id', 'lawd_code', 'contract_date_from', 'contract_date_to'}:
                raise ValueError('unsupported coverage filters')
            if any(not isinstance(v, str) for v in filters.values()):
                raise ValueError('coverage filters require strings')
            if type(limit) is not int or not 1 <= limit <= 1000:
                raise ValueError('invalid limit')
            from datetime import date
            for name in ('contract_date_from', 'contract_date_to'):
                if name in filters:
                    date.fromisoformat(filters[name])
            digest = manifest_digest(manifest)
            query_digest = hashlib.sha256(_json(['coverage', filters])).hexdigest()
            offset = query._decode(cursor, digest, query_digest) if cursor is not None else 0
            if query.inventory is None:
                result = {'coverage': manifest['coverage'], 'total': 0, 'rows': [],
                          'warnings': ['Legacy coverage has no exact service-region-month evidence.']}
            else:
                coverage = query.inventory.coverage(filters)
                keys = {key(p): p for name in ('declared', 'observed') for p in coverage[name]}
                page = [keys[k] for k in sorted(keys)][offset:offset + limit]
                page_keys = {key(p) for p in page}
                paged = {name: [p for p in coverage[name] if key(p) in page_keys]
                         for name in ('declared', 'observed', 'zero', 'not_collected')}
                paged.update(scope=coverage['scope'], stale=coverage['stale'],
                             complete=coverage['complete'], gaps_overlapping_query=paged['not_collected'])
                parts = [p for p in query.inventory.select(filters) if key(p) in page_keys]
                result = {'coverage': paged, 'rows': page, 'total': len(keys),
                          'partition_provenance': query.inventory.provenance(parts),
                          'next_cursor': query._encode(digest, query_digest, offset + len(page))
                              if offset + len(page) < len(keys) else None}
        else:
            result = query.search(filters, limit=limit, cursor=cursor)
    except SnapshotIntegrityError:
        raise ActiveSnapshotError("active_snapshot_unverified") from None
    except Exception:
        # A read may fail before post-verification (including Arrow/DuckDB IO).
        # Recheck integrity before treating the original error as invalid input.
        try:
            verify_local(root, manifest)
        except Exception:
            raise ActiveSnapshotError("active_snapshot_unverified") from None
        raise
    # Detect changes during the search before releasing rows to the caller.
    try:
        verify_local(root, manifest)
    except Exception:
        raise ActiveSnapshotError("active_snapshot_unverified") from None
    result.update(release_state="active", verification={"complete": True, "hashes_verified": True,
                  "rows_verified": True, "scope": "full-current-inventory-pre-post",
                  "archive_checked": False, "files_checked": len(query.inventory.entries) if query.inventory else len(manifest["files"])})
    result.update(snapshot_id=manifest['release_id'], snapshot_digest=manifest_digest(manifest),
                  public_revisions=manifest['hugging_face_revisions'], legal_verified=False)
    return manifest, result


def coverage_active(settings, filters=None, *, limit=100, cursor=None):
    return search_active(settings, {} if filters is None else filters,
                         limit=limit, cursor=cursor, coverage_only=True)
