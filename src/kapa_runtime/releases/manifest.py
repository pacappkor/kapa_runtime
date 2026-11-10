# SPDX-License-Identifier: GPL-3.0-only
"""Strict, canonical release manifests. Hash checks never confer legal approval."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from kapa_runtime.models import ReleaseState, SCHEMA_VERSION

SHA = re.compile(r"[0-9a-fA-F]{40}\Z")
HASH = re.compile(r"[0-9a-f]{64}\Z")


class ManifestError(ValueError):
    """Invalid release contract."""


def relative_path(value: str) -> str:
    """Reject ambiguous POSIX paths, including Windows drive/ADS syntax."""
    if (not isinstance(value, str) or not value or "\\" in value or ":" in value
            or any(ord(c) < 32 for c in value) or value.startswith("/")
            or any(p in ("", ".", "..") or p.endswith((" ", ".")) for p in value.split("/"))):
        raise ManifestError("Invalid relative POSIX file path")
    if PurePosixPath(value).is_absolute():
        raise ManifestError("Invalid relative POSIX file path")
    return value


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ManifestError("Duplicate JSON key")
        result[key] = value
    return result


def _constant(value):
    raise ManifestError("Non-finite JSON number")


def _json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ManifestError("Manifest must contain finite JSON values") from exc


def validate_manifest(manifest: dict) -> dict:
    """Validate and return a detached canonical object; never silently repair input."""
    m = json.loads(_json(manifest), object_pairs_hook=_pairs, parse_constant=_constant)
    if not isinstance(m, dict):
        raise ManifestError("Manifest must be an object")
    required = {"release_id", "state", "created_at", "source_runs", "github_revisions",
                "hugging_face_revisions", "schema_version", "parser_version", "data_schema_version",
                "files", "coverage", "quality", "unresolved_gaps"}
    if not required <= m.keys():
        raise ManifestError("Missing manifest fields")
    try:
        state = ReleaseState(m["state"])
        created = datetime.fromisoformat(m["created_at"])
        if created.utcoffset() is None:
            raise ValueError
    except (ValueError, TypeError):
        raise ManifestError("Invalid state or timezone-aware creation time") from None
    for field in ("release_id", "schema_version", "parser_version", "data_schema_version"):
        if not isinstance(m[field], str) or not m[field].strip():
            raise ManifestError("Missing manifest identifier/version")
    if "normalized_extra_fields" in m:
        allowlists = m["normalized_extra_fields"]
        if (not isinstance(allowlists, dict)
                or not all(isinstance(service, str) and re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", service)
                           and isinstance(fields, list)
                           and all(isinstance(name, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
                                   for name in fields)
                           and len(fields) == len(set(fields))
                           for service, fields in allowlists.items())
                or not isinstance(m["coverage"], dict)
                or not isinstance(m["coverage"].get("services"), list)
                or not all(isinstance(service, str) for service in m["coverage"]["services"])
                or set(allowlists) != set(m["coverage"]["services"])):
            raise ManifestError("Invalid normalization extra-field allowlists")
    for field in ("source_runs", "unresolved_gaps"):
        if not isinstance(m[field], list) or not all(isinstance(x, str) for x in m[field]):
            raise ManifestError("Invalid manifest list")
    if not isinstance(m["coverage"], dict) or not isinstance(m["quality"], dict):
        raise ManifestError("Invalid coverage or quality")
    if m["quality"].get("legal_verified") is not False or m.get("legal_verified", False) is not False:
        raise ManifestError("legal_verified must always be false")
    if not isinstance(m["github_revisions"], dict) or not isinstance(m["hugging_face_revisions"], list):
        raise ManifestError("Invalid revision collections")
    fixed = state in (ReleaseState.PUBLISHED, ReleaseState.ACTIVE)
    revisions = list(m["github_revisions"].values())
    repos = []
    for ref in m["hugging_face_revisions"]:
        if not isinstance(ref, dict) or set(ref) != {"repo_id", "commit_sha"}:
            raise ManifestError("Invalid HF revision")
        if not isinstance(ref["repo_id"], str) or not re.fullmatch(r"[\w.-]+/[\w.-]+", ref["repo_id"]):
            raise ManifestError("Invalid HF repository ID")
        repos.append(ref["repo_id"])
        revisions.append(ref["commit_sha"])
    if "staging_revision" in m:
        ref = m["staging_revision"]
        if (not isinstance(ref, dict) or set(ref) != {"repo_id", "commit_sha"}
                or not isinstance(ref["repo_id"], str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", ref["repo_id"])
                or not isinstance(ref["commit_sha"], str) or not SHA.fullmatch(ref["commit_sha"])):
            raise ManifestError("Invalid staging revision")
    if len(set(repos)) != len(repos):
        raise ManifestError("Duplicate HF repository")
    if fixed and (not repos or not m["github_revisions"]):
        raise ManifestError("Published releases require source and dataset revisions")
    if any(not (isinstance(v, str) and SHA.fullmatch(v)) for v in revisions if fixed or v is not None):
        raise ManifestError("Revisions must be full 40-hex commit SHAs")
    if not isinstance(m["files"], list):
        raise ManifestError("Invalid file list")
    paths = []
    for entry in m["files"]:
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256", "rows"}:
            raise ManifestError("Invalid file entry")
        paths.append(relative_path(entry["path"]))
        if not isinstance(entry["sha256"], str) or not HASH.fullmatch(entry["sha256"]):
            raise ManifestError("Invalid SHA-256")
        if type(entry["rows"]) is not int or entry["rows"] < 0:
            raise ManifestError("Rows must be a nonnegative integer")
    if len(set(paths)) != len(paths) or paths != sorted(paths):
        raise ManifestError("Manifest file paths must be unique and sorted")
    if fixed and not paths:
        raise ManifestError("Published releases require files")
    return m


def build_manifest(*, release_id: str, files: list[dict], parser_version: str,
                   source_runs=(), github_revisions=None, hugging_face_revisions=(),
                   coverage=None, quality=None, unresolved_gaps=(),
                   state=ReleaseState.DRAFT, created_at: datetime | str | None = None,
                   data_schema_version=SCHEMA_VERSION) -> dict:
    created_at = created_at or datetime.now(timezone.utc)
    m = dict(schema_version="kapa-runtime-release/1", release_id=release_id, state=str(state),
             created_at=created_at.isoformat() if isinstance(created_at, datetime) else created_at,
             source_runs=list(source_runs), github_revisions=dict(github_revisions or {}),
             hugging_face_revisions=list(hugging_face_revisions), parser_version=parser_version,
             data_schema_version=data_schema_version, files=sorted(files, key=lambda f: f["path"]),
             coverage=dict(coverage or {}), quality={"passed": False, "legal_verified": False, **(quality or {})},
             unresolved_gaps=list(unresolved_gaps), legal_verified=False)
    return validate_manifest(m)


def dumps_manifest(manifest: dict) -> str:
    return _json(validate_manifest(manifest)) + "\n"


def loads_manifest(text: str) -> dict:
    try:
        m = json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant)
    except (ValueError, TypeError) as exc:
        raise ManifestError("Invalid strict JSON manifest") from exc
    return validate_manifest(m)


def load_manifest(path: Path) -> dict:
    """Read a manifest pointer only after checking its leaf and all ancestors."""
    from ..storage.metadata import read_metadata
    return loads_manifest(read_metadata(path))


def dump_manifest(manifest: dict, path: Path) -> None:
    Path(path).write_text(dumps_manifest(manifest), encoding="utf-8", newline="\n")


def manifest_digest(manifest: dict) -> str:
    return hashlib.sha256(dumps_manifest(manifest).encode("utf-8")).hexdigest()
