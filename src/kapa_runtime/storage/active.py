# SPDX-License-Identifier: GPL-3.0-only
"""Credential-free, read-only access to the ledger-bound active snapshot."""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import re
import sqlite3

import pyarrow.parquet as pq

from ..releases.manifest import load_manifest, manifest_digest, relative_path
from .parquet import _safe_path, read_partition, schema_for
from .query import TransactionQuery


class ActiveSnapshotError(ValueError):
    """Fixed, value-free read failure."""


def verify_local(root, manifest):
    """Check the complete file inventory, bytes, schema and row counts."""
    _safe_path(root)
    version = manifest["data_schema_version"]
    expected_schema = schema_for(version)
    expected = {f["path"] for f in manifest["files"] if f["path"].endswith(".parquet")}
    actual = set()
    for path in root.rglob("*"):
        _safe_path(path)
        if path.is_file() and path.suffix == ".parquet":
            actual.add(path.relative_to(root).as_posix())
    if expected != actual:
        raise ActiveSnapshotError("snapshot_file_set_mismatch")
    for entry in manifest["files"]:
        path = root / relative_path(entry["path"])
        _safe_path(path)
        if hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
            raise ActiveSnapshotError("local_hash_mismatch")
        if path.suffix == ".parquet":
            if not pq.read_schema(path).equals(expected_schema, check_metadata=True):
                raise ActiveSnapshotError("schema_mismatch")
            if len(read_partition(path, schema_version=version)) != entry["rows"]:
                raise ActiveSnapshotError("row_count_mismatch")


def search_active(settings, filters, *, limit=100, cursor=None):
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
    result = TransactionQuery(root, data_schema_version=manifest["data_schema_version"]).search(
        filters, limit=limit, cursor=cursor)
    # Detect changes during the search before releasing rows to the caller.
    try:
        verify_local(root, manifest)
    except Exception:
        raise ActiveSnapshotError("active_snapshot_unverified") from None
    result.update(release_state="active", verification={"complete": True, "hashes_verified": True,
                  "rows_verified": True, "files_checked": len(manifest["files"])})
    return manifest, result
