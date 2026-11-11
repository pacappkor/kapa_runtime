# SPDX-License-Identifier: GPL-3.0-only
"""Single-writer atomic manifest activation and one-step rollback."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .hf import VerificationReport
from .manifest import dumps_manifest, load_manifest, manifest_digest


class ActivationError(ValueError):
    """Candidate cannot replace the current active reference."""


def _atomic_write(path: Path, payload: bytes) -> None:
    fd, name = tempfile.mkstemp(prefix=".release-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _selected_paths(manifest, root):
    from .snapshot import inventory
    return {f['path'] for f in inventory(manifest, root)} | {
        r['path'] for r in manifest.get('coverage_shards', [])}


def activate(manifest_path: Path, active_path: Path, verified: VerificationReport, *, snapshot_root=None) -> None:
    """Activate the exact verified published manifest, not a mutable candidate.

    The pointer filename denotes activation; preserve the verified manifest bytes
    and its published state so the report remains bound to the active content.
    Callers serialize writers (as for the runtime state database).
    """
    from .hf import VerificationReport

    manifest = load_manifest(manifest_path)
    refs = {(r["repo_id"], r["commit_sha"]) for r in manifest["hugging_face_revisions"]}
    paths = _selected_paths(manifest, snapshot_root or Path(manifest_path).parent.parent)
    if (not isinstance(verified, VerificationReport) or not verified.passed
            or manifest["state"] != "published"
            or verified.manifest_sha256 != manifest_digest(manifest)
            or set(verified.revisions) != refs or set(verified.checked_files) != paths):
        raise ActivationError("Complete verification of this published manifest is required")
    active_path = Path(active_path)
    previous = active_path.with_name("previous.json")
    if active_path == previous:
        raise ActivationError("Active and previous paths must differ")
    active_path.parent.mkdir(parents=True, exist_ok=True)
    if active_path.exists():
        # Validate the backup before changing either reference.
        old = load_manifest(active_path)
        if manifest_digest(old) == manifest_digest(manifest):
            # Recovery after pointer replacement but before the ledger commit.
            # Do not replace the genuine rollback target with this candidate.
            return
        _atomic_write(previous, dumps_manifest(old).encode("utf-8"))
    _atomic_write(active_path, dumps_manifest(manifest).encode("utf-8"))


def rollback(active_path: Path, verified: VerificationReport, *, snapshot_root=None) -> None:
    """Restore the prior reference; retain previous.json and every data file."""
    from .hf import VerificationReport

    active_path = Path(active_path)
    previous = active_path.with_name("previous.json")
    if active_path == previous:
        raise ActivationError("Active and previous paths must differ")
    old = load_manifest(previous)
    if (old["state"] not in ("published", "active")
            or not isinstance(verified, VerificationReport) or not verified.passed
            or verified.manifest_sha256 != manifest_digest(old)
            or set(verified.revisions) != {(r["repo_id"], r["commit_sha"]) for r in old["hugging_face_revisions"]}
            or set(verified.checked_files) != _selected_paths(old, snapshot_root or previous.parent)):
        raise ActivationError("Complete verification of the previous release is required")
    _atomic_write(active_path, dumps_manifest(old).encode("utf-8"))
