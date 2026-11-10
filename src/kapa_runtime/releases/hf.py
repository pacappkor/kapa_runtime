# SPDX-License-Identifier: GPL-3.0-only
"""Private-only, injected HF publishing and revision-pinned verification."""
from __future__ import annotations

import base64
import hashlib
import os
from urllib.parse import quote, quote_plus, unquote_to_bytes

import pyarrow as pa
import httpx
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

import pyarrow.parquet as pq
from huggingface_hub import CommitOperationAdd, CommitOperationDelete
from huggingface_hub.errors import RepositoryNotFoundError

from .manifest import SHA, manifest_digest, relative_path, validate_manifest


class PublishError(ValueError):
    """Safe publication failure with a fixed, non-sensitive reason category."""

    REASONS = frozenset({"hub_metadata_invalid", "hub_auth", "hub_not_found",
                         "hub_rate_limited", "hub_server", "hub_network", "local_scan", "unknown"})

    def __init__(self, message, *, reason="unknown"):
        super().__init__(message)
        self.reason = reason if reason in self.REASONS else "unknown"


def _error_reason(exc):
    """Inspect only type/status, except the Hub's fixed metadata sentinel.

    No upstream message, URL, response body or headers leave this boundary.
    """
    if isinstance(exc, PublishError):
        return exc.reason
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status in (401, 403):
        return "hub_auth"
    if status == 404 or isinstance(exc, RepositoryNotFoundError):
        return "hub_not_found"
    if status == 429:
        return "hub_rate_limited"
    if isinstance(status, int) and 500 <= status < 600:
        return "hub_server"
    if isinstance(exc, httpx.TransportError):
        return "hub_network"
    # huggingface_hub raises this plain ValueError before HTTP commit creation.
    if isinstance(exc, ValueError) and str(exc).startswith("Invalid metadata in README.md"):
        return "hub_metadata_invalid"
    return "unknown"


@dataclass(frozen=True)
class VerificationReport:
    manifest_sha256: str
    revisions: tuple[tuple[str, str], ...]
    checked_files: tuple[str, ...]
    errors: tuple[str, ...] = ()
    # Entire pinned tree, separate from the candidate manifest's checked files.
    verified_inventory: tuple[tuple[str, str, str], ...] = ()

    @property
    def passed(self) -> bool:
        return not self.errors and bool(self.revisions) and bool(self.checked_files)

    @classmethod
    def combine(cls, *reports: VerificationReport) -> VerificationReport:
        if not reports or len({r.manifest_sha256 for r in reports}) != 1:
            raise PublishError("Reports must verify the same manifest")
        return cls(reports[0].manifest_sha256,
                   tuple(sorted({ref for r in reports for ref in r.revisions})),
                   tuple(sorted({f for r in reports for f in r.checked_files})),
                   tuple(e for r in reports for e in r.errors))


def _no_links(path: Path) -> None:
    for component in (path, *path.parents):
        info = component.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise PublishError("Symlinks and reparse points are forbidden")


MAX_DECODED_ROWS = 2_000_000
MAX_DECODED_STRING_BYTES = 256 * 1024 * 1024
MAX_PARQUET_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
MAX_ROW_GROUP_UNCOMPRESSED_BYTES = 32 * 1024 * 1024
MAX_COLUMN_UNCOMPRESSED_BYTES = 16 * 1024 * 1024
MAX_DICTIONARY_PAGE_BYTES = 8 * 1024 * 1024
PARQUET_BATCH_ROWS = 128


def _check_parquet_expansion(metadata):
    """Reject advertised expansion before Arrow decodes any data pages."""
    def bounded(value, cap):
        if type(value) is not int or value < 0 or value > cap:
            raise PublishError('Parquet decoded limits exceeded', reason='local_scan')
    bounded(metadata.num_rows, MAX_DECODED_ROWS)
    total = 0
    for index in range(metadata.num_row_groups):
        group = metadata.row_group(index)
        bounded(group.total_byte_size, MAX_ROW_GROUP_UNCOMPRESSED_BYTES)
        columns = 0
        for column_index in range(group.num_columns):
            column = group.column(column_index)
            size = column.total_uncompressed_size
            bounded(size, MAX_COLUMN_UNCOMPRESSED_BYTES)
            columns += size
            # Arrow exposes the dictionary offset, not its decoded page size.
            # Bounding its entire uncompressed chunk conservatively bounds it.
            if column.has_dictionary_page:
                bounded(size, MAX_DICTIONARY_PAGE_BYTES)
                dictionary = column.dictionary_page_offset
                data = column.data_page_offset
                bounded(dictionary, 2**63 - 1)
                bounded(data - dictionary, MAX_DICTIONARY_PAGE_BYTES)
        bounded(columns, MAX_ROW_GROUP_UNCOMPRESSED_BYTES)
        total += max(group.total_byte_size, columns)
        bounded(total, MAX_PARQUET_UNCOMPRESSED_BYTES)


def scan_secret_payload(payload: bytes, name: str, secrets: Iterable, *, schema_version="kapa-runtime-data/2") -> None:
    """Scan common wire encodings and decoded compressed Parquet strings."""
    patterns = set()
    hex_patterns = set()
    for secret in secrets:
        value = secret.reveal()
        raw = value.encode("utf-8")
        if not raw:
            raise PublishError("Empty secret scan value")
        hex_patterns.add(raw.hex().encode())
        patterns.update((raw, quote(value, safe="").encode(), quote_plus(value, safe="").encode(),
                         value.encode("utf-16-le"), value.encode("utf-16-be"),
                         raw.hex().encode(), raw.hex().upper().encode(),
                         base64.b64encode(raw), base64.urlsafe_b64encode(raw),
                         base64.b64encode(raw).rstrip(b"="), base64.urlsafe_b64encode(raw).rstrip(b"=")))
    patterns.update(unquote_to_bytes(pattern) for pattern in tuple(patterns))
    patterns = {re.sub(rb"%[0-9a-fA-F]{2}", lambda m: m[0].upper(), pattern) for pattern in patterns}
    def check(data):
        canonical = re.sub(rb"%[0-9a-fA-F]{2}", lambda m: m[0].upper(), data)
        views = (data, canonical, unquote_to_bytes(canonical))
        if (any(pattern in view for view in views for pattern in patterns)
                or any(pattern in view.lower() for view in views for pattern in hex_patterns)):
            raise PublishError("Upload contains a configured secret", reason="local_scan")
    check(payload)
    if name.lower().endswith(".parquet"):
        try:
            from ..storage.parquet import schema_for
            reader = pq.ParquetFile(pa.BufferReader(payload))
            if not reader.schema_arrow.equals(schema_for(schema_version), check_metadata=True):
                raise PublishError("Parquet schema outside runtime allowlist", reason="local_scan")
            _check_parquet_expansion(reader.metadata)
            rows = string_bytes = 0
            for batch in reader.iter_batches(batch_size=PARQUET_BATCH_ROWS):
                rows += batch.num_rows
                if rows > MAX_DECODED_ROWS:
                    raise PublishError("Parquet decoded limits exceeded", reason="local_scan")
                for column in batch.columns:
                    if pa.types.is_string(column.type) or pa.types.is_large_string(column.type):
                        for scalar in column:
                            value = scalar.as_py()
                            if value is not None:
                                encoded = value.encode("utf-8")
                                string_bytes += len(encoded)
                                if string_bytes > MAX_DECODED_STRING_BYTES:
                                    raise PublishError("Parquet decoded limits exceeded", reason="local_scan")
                                check(encoded)
        except PublishError:
            raise
        except Exception:
            raise PublishError("Parquet secret inspection failed", reason="local_scan") from None


MAX_DOWNLOAD_BYTES = 256 * 1024 * 1024


def _clean_download(cache, repo_id, revision, name, info, downloaded):
    """Best-effort removal of this file's snapshot, blob and partial cache entries."""
    repo = cache / ('datasets--' + repo_id.replace('/', '--'))
    candidates = [cache / name, repo / 'snapshots' / revision / name]
    if downloaded is not None:
        candidates.append(Path(downloaded))
    blob_id = getattr(getattr(info, 'lfs', None), 'sha256', None) or getattr(info, 'blob_id', None)
    if isinstance(getattr(info, 'lfs', None), dict):
        blob_id = info.lfs.get('sha256') or blob_id
    if isinstance(blob_id, str) and re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', blob_id):
        candidates.extend([repo / 'blobs' / blob_id, repo / 'blobs' / (blob_id + '.incomplete')])
    for candidate in candidates:
        try:
            target = candidate.resolve()
            if target != cache and target.is_relative_to(cache):
                target.unlink(missing_ok=True)
            if candidate.parent.resolve().is_relative_to(cache) and candidate.is_symlink():
                candidate.unlink(missing_ok=True)
        except (OSError, ValueError, RuntimeError):
            pass


def download_bytes(api, repo_id, name, *, revision, cache_dir, token):
    """Preflight pinned size, then read a bounded regular cache blob."""
    if not isinstance(revision, str) or not SHA.fullmatch(revision) or cache_dir is None:
        raise PublishError("Pinned revision and explicit cache required")
    relative_path(name)
    cache = Path(cache_dir).resolve()
    downloaded = info = None
    try:
        entries = api.get_paths_info(repo_id, [name], repo_type='dataset', revision=revision,
                                     expand=True, token=token)
        if len(entries) != 1 or getattr(entries[0], 'path', None) != name:
            raise PublishError("Pinned file size metadata required")
        info = entries[0]
        size = getattr(info, 'size', None)
        if type(size) is not int or size < 0 or size > MAX_DOWNLOAD_BYTES:
            raise PublishError("Download requires a bounded regular file")
        downloaded = api.hf_hub_download(repo_id, name, repo_type="dataset", revision=revision,
            cache_dir=cache_dir, token=token, force_download=True)
        path = Path(downloaded).resolve(strict=True)
        if path == cache or not path.is_relative_to(cache):
            raise PublishError("Download target must stay inside cache")
        checked = path.lstat()
        if not stat.S_ISREG(checked.st_mode) or checked.st_size > MAX_DOWNLOAD_BYTES:
            raise PublishError("Download requires a bounded regular file")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
        with os.fdopen(os.open(path, flags), "rb") as stream:
            opened = os.fstat(stream.fileno())
            identity = (checked.st_dev, checked.st_ino)
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != identity:
                raise PublishError("Download changed during open")
            payload = stream.read(MAX_DOWNLOAD_BYTES + 1)
            current = path.lstat()
            if (len(payload) > MAX_DOWNLOAD_BYTES or path.resolve(strict=True) != path
                    or (current.st_dev, current.st_ino) != identity or len(payload) != size):
                raise PublishError("Download changed during read")
        return payload
    except Exception:
        _clean_download(cache, repo_id, revision, name, info, downloaded)
        raise


_PROMOTION_SEAL = object()


class _VerifiedPromotion:
    def __init__(self, seal, repo_id, payloads):
        if seal is not _PROMOTION_SEAL:
            raise PublishError("Verified promotion intent required")
        self._repo_id = repo_id
        self._hashes = {name: hashlib.sha256(value).hexdigest() for name, value in payloads.items()}

    def _matches(self, repo_id, payloads):
        return self._repo_id == repo_id and self._hashes == {
            name: hashlib.sha256(value).hexdigest() for name, value in payloads.items()}


def _verified_promotion(repo_id, staging, report, manifest, payloads):
    if (not staging.expected_private or not report.passed
            or report.manifest_sha256 != manifest_digest(manifest)
            or set(report.revisions) != {(r["repo_id"], r["commit_sha"]) for r in manifest["hugging_face_revisions"]}
            or any(repo != staging.repo_id for repo, _ in report.revisions)
            or set(report.checked_files) != {f["path"] for f in manifest["files"]}
            or {name: digest for name, digest, _ in report.verified_inventory} != {
                name: hashlib.sha256(value).hexdigest() for name, value in payloads.items()}
            or not {f["path"] for f in manifest["files"]} <= set(payloads)
            or any(hashlib.sha256(payloads[f["path"]]).hexdigest() != f["sha256"] for f in manifest["files"])):
        raise PublishError("Complete private verification required for promotion")
    return _VerifiedPromotion(_PROMOTION_SEAL, repo_id, payloads)


class _HubWriter:
    def __init__(self, api, namespace: str, repo_name: str, *,
                 role: str, terms_evidence=None,
                 secret_values: Callable[[], Iterable] = lambda: (), expected_visibility="private",
                 restoration=None):
        if not all(isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", v)
                   for v in (namespace, repo_name)):
            raise PublishError("Explicit namespace and repository name required")
        if expected_visibility not in ("private", "public"):
            raise PublishError("Explicit valid visibility required")
        if not isinstance(role, str) or not role.strip():
            raise PublishError("Explicit repository role required")
        restore_payloads = None
        if restoration is not None:
            from ..storage.state import _RestorationAuthority
            if type(restoration) is not _RestorationAuthority:
                raise PublishError("State-store restoration authority required")
            restore_payloads, _ = restoration._inventory(f"{namespace}/{repo_name}")
        self._restoration = restoration
        if expected_visibility == "public" and (
                role != "market" or restore_payloads is None and (not isinstance(terms_evidence, dict) or not terms_evidence
                or any(not isinstance(entry, dict) or entry.get("terms_reviewed") is not True
                       or not isinstance(entry.get("terms_basis"), str) or not entry["terms_basis"].strip()
                       for entry in terms_evidence.values()))):
            raise PublishError("Public market publication requires reviewed terms evidence")
        from ..credentials import Secret
        self._token = getattr(api, "token", False)
        if hasattr(api, "token") and (not isinstance(self._token, str) or not self._token.strip()):
            raise PublishError("API must carry an explicit token, not ambient credentials")
        loaded_secrets = tuple(secret_values())
        if not loaded_secrets:
            raise PublishError("Nonempty secret scan set required", reason="local_scan")
        if isinstance(self._token, str):
            loaded_secrets += (Secret(self._token),)
        # Validate every needle now, including empty values, before any Hub call.
        scan_secret_payload(b"", "scan-validation", loaded_secrets)
        for identifier in (namespace, repo_name, f"{namespace}/{repo_name}"):
            scan_secret_payload(identifier.encode("utf-8"), "repository-identifier", loaded_secrets)
        self._restore_payloads = dict(restore_payloads) if restore_payloads is not None else None
        self.expected_private = expected_visibility == "private"
        self.api = api
        self.repo_id = f"{namespace}/{repo_name}"
        self.secret_values = lambda: loaded_secrets

    def _private(self) -> None:
        info = self.api.repo_info(self.repo_id, repo_type="dataset", token=self._token)
        if getattr(info, "private", None) is not self.expected_private:
            raise PublishError("Dataset visibility differs from explicit configuration")

    def ensure_private_repo(self) -> None:
        if self._restore_payloads is not None:
            raise PublishError("Restore-only publisher cannot create repositories")
        try:
            self._private()
        except RepositoryNotFoundError as exc:
            # HF also uses this error for inaccessible repos; only a genuine 404
            # permits a create attempt. exist_ok=False prevents a race from hiding it.
            if exc.response is None or exc.response.status_code != 404:
                raise PublishError("Dataset access could not be established", reason=_error_reason(exc)) from None
            try:
                self.api.create_repo(self.repo_id, repo_type="dataset", private=self.expected_private,
                                     exist_ok=False, token=self._token)
                self._private()
            except Exception as exc:
                raise PublishError("Dataset creation failed", reason=_error_reason(exc)) from None
        except PublishError:
            raise
        except Exception as exc:
            raise PublishError("Dataset visibility check failed", reason=_error_reason(exc)) from None

    def upload_candidate(self, staging_dir: Path, allowlist: list[str], *, exact_inventory=False,
                         evidence_store=None, cache_dir=None, previous_files=(), repair_retained=None) -> str:
        if not self.expected_private or self._restore_payloads is not None:
            raise PublishError("Candidate uploads require a private publication writer")
        if not allowlist or len(set(allowlist)) != len(allowlist):
            raise PublishError("A nonempty unique allowlist is required")
        staging = Path(staging_dir).absolute()
        operations = []
        try:
            _no_links(staging)
            secrets = list(self.secret_values())
            for name in sorted(allowlist):
                self._scan_path(name)
                path = staging.joinpath(*name.split("/"))
                _no_links(path)
                if not path.resolve().is_relative_to(staging.resolve()) or not path.is_file():
                    raise PublishError("Upload must be a regular file inside staging")
                payload = path.read_bytes()
                scan_secret_payload(payload, name, secrets)
                # Commit the scanned bytes, never reopen a mutable path at upload time.
                operations.append(CommitOperationAdd(path_in_repo=name, path_or_fileobj=payload))
            if exact_inventory:
                payloads = {op.path_in_repo: op.path_or_fileobj for op in operations}
                parent, remote = self._head_tree()
                retained = {}
                payload_versions = {}
                for entry in previous_files:
                    if (not isinstance(entry, dict) or set(entry) not in ({"path", "sha256", "rows"}, {"path", "sha256", "rows", "data_schema_version"})
                            or not isinstance(entry["sha256"], str)
                            or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
                            or type(entry["rows"]) is not int or entry["rows"] < 0):
                        raise PublishError("Retained files require manifest evidence")
                    self._scan_path(entry["path"])
                    if entry["path"] not in payloads:
                        if entry["path"] in retained and retained[entry["path"]] != [entry]:
                            raise PublishError("Ambiguous retained path ownership")
                        retained[entry["path"]] = [entry]
                for name, entries in retained.items():
                    payload = None
                    try:
                        candidate = download_bytes(self.api, self.repo_id, name,
                            revision=parent, token=self._token, cache_dir=cache_dir)
                        digest = hashlib.sha256(candidate).hexdigest()
                        owner = next((e for e in entries if e['sha256'] == digest), None)
                        if owner is None:
                            raise PublishError('Retained file mismatch')
                        version = owner.get('data_schema_version', 'kapa-runtime-data/2')
                        scan_secret_payload(candidate, name, secrets, schema_version=version)
                        digest = hashlib.sha256(candidate).hexdigest()
                        rows = pq.read_metadata(pa.BufferReader(candidate)).num_rows if name.lower().endswith(".parquet") else 0
                        if any(e["sha256"] == digest and e["rows"] == rows for e in entries):
                            payload = candidate
                    except Exception:
                        pass
                    if payload is None:
                        if repair_retained is None:
                            raise PublishError("Retained file mismatch")
                        payload = repair_retained(name, entries)
                    if payload is not None:
                        digest = hashlib.sha256(payload).hexdigest()
                        owner = next((e for e in entries if e['sha256'] == digest), None)
                        if owner is None:
                            raise PublishError('Local restoration evidence mismatch')
                        version = owner.get('data_schema_version', 'kapa-runtime-data/2')
                        scan_secret_payload(payload, name, secrets, schema_version=version)
                        payload_versions[name] = version
                        digest = hashlib.sha256(payload).hexdigest()
                        rows = pq.read_metadata(pa.BufferReader(payload)).num_rows if name.lower().endswith(".parquet") else 0
                        if not any(e["sha256"] == digest and e["rows"] == rows for e in entries):
                            raise PublishError("Local restoration evidence mismatch")
                        payloads[name] = payload
                return self._replace_files(payloads, expected_parent=parent, payload_versions=payload_versions,
                                          evidence_store=evidence_store, cache_dir=cache_dir)
            self._private()  # Immediately before every upload, even after ensure.
            result = self.api.create_commit(self.repo_id, operations=operations,
                                           repo_type="dataset", token=self._token,
                                           commit_message="Upload validated release candidate")
            if not isinstance(result.oid, str) or not SHA.fullmatch(result.oid):
                raise PublishError("Hub did not return a full commit SHA")
            return result.oid
        except PublishError:
            raise
        except Exception as exc:
            raise PublishError("Candidate upload failed", reason=_error_reason(exc)) from None

    def _scan_path(self, name):
        relative_path(name)
        for value in (name, quote(name, safe=""), quote_plus(name, safe="")):
            scan_secret_payload(value.encode("utf-8"), "upload-path", self.secret_values())
        if re.search(r"key|token|secret|\.env", name, re.IGNORECASE):
            raise PublishError("Credential-like filenames are forbidden", reason="local_scan")

    def _require_head(self, expected):
        info = self.api.repo_info(self.repo_id, repo_type="dataset", token=self._token)
        if getattr(info, "private", None) is not self.expected_private:
            raise PublishError("Dataset visibility differs from explicit configuration")
        if getattr(info, "sha", None) != expected:
            raise PublishError("Default-branch head changed")

    def _head_tree(self):
        info = self.api.repo_info(self.repo_id, repo_type="dataset", token=self._token)
        if getattr(info, "private", None) is not self.expected_private:
            raise PublishError("Dataset visibility differs from explicit configuration")
        sha = getattr(info, "sha", None)
        if not isinstance(sha, str) or not SHA.fullmatch(sha):
            raise PublishError("Full default-branch head SHA required")
        remote = self.api.list_repo_files(self.repo_id, repo_type="dataset", revision=sha, token=self._token)
        if not isinstance(remote, list) or len(set(remote)) != len(remote):
            raise PublishError("Complete remote inventory required")
        for name in remote:
            relative_path(name)
        return sha, remote

    def _promote_verified(self, staging, report, manifest, payloads, *, expected_parent,
                          evidence_store, cache_dir):
        intent = _verified_promotion(self.repo_id, staging, report, manifest, payloads)
        return self._replace_files(payloads, expected_parent=expected_parent,
            evidence_store=evidence_store, cache_dir=cache_dir, _promotion=intent,
            payload_versions={name: version for name, _, version in report.verified_inventory})

    def _journal_schema(self, payload, name):
        if self._restoration is not None and name.lower().endswith(".parquet"):
            from ..storage.parquet import schema_for
            version = (pq.read_schema(pa.BufferReader(payload)).metadata or {}).get(b"schema_version", b"").decode()
            schema_for(version)
            return version
        return "kapa-runtime-data/2"

    def _restore_journal(self):
        if self._restoration is None:
            raise PublishError("State-store restoration authority required")
        payloads, parent = self._restoration._inventory(self.repo_id)
        return self._replace_files(payloads, preserve_attributes=False, expected_parent=parent)

    def _replace_files(self, payloads: dict[str, bytes], *, preserve_attributes=True,
                      evidence_store=None, cache_dir=None, expected_parent=None, _promotion=None, payload_versions=None) -> str:
        """Replace the enumerated tree, refusing concurrent default-branch changes."""
        payloads = dict(payloads)
        if not self.expected_private and self._restoration is None:
            if type(_promotion) is not _VerifiedPromotion or not _promotion._matches(self.repo_id, payloads):
                raise PublishError("Public writes require private-verified promotion intent")
        if self._restoration is not None:
            journaled, parent = self._restoration._inventory(self.repo_id)
            if payloads != journaled or expected_parent != parent:
                raise PublishError("Restoration differs from persisted journal")
        if self._restore_payloads is not None and (payloads != self._restore_payloads or preserve_attributes):
            raise PublishError("Restore-only publisher requires exact journaled inventory")
        for name, payload in payloads.items():
            self._scan_path(name)
            scan_secret_payload(payload, name, self.secret_values(),
                                schema_version=(payload_versions or {}).get(name, self._journal_schema(payload, name)))
        if expected_parent is not None:
            self._require_head(expected_parent)
        parent, remote = self._head_tree()
        if expected_parent is not None and parent != expected_parent:
            raise PublishError("Public baseline changed before promotion")
        if preserve_attributes:
            digest = None
            if ".gitattributes" in remote:
                if evidence_store is None:
                    raise PublishError("Durable repository evidence required")
                payload = download_bytes(self.api, self.repo_id, ".gitattributes",
                    revision=parent, cache_dir=cache_dir, token=self._token)
                scan_secret_payload(payload, ".gitattributes", self.secret_values())
                digest = hashlib.sha256(payload).hexdigest()
            if evidence_store is not None:
                evidence_store.bind_repository_attributes(self.repo_id, digest)
        operations = [CommitOperationDelete(path_in_repo=name) for name in remote
                      if name not in payloads and not (preserve_attributes and name == ".gitattributes")]
        operations.extend(CommitOperationAdd(path_in_repo=name, path_or_fileobj=payload)
                          for name, payload in sorted(payloads.items()))
        self._private()
        result = self.api.create_commit(self.repo_id, operations=operations, repo_type="dataset",
            token=self._token, parent_commit=parent, commit_message="Replace verified release inventory")
        if not isinstance(result.oid, str) or not SHA.fullmatch(result.oid):
            raise PublishError("Hub did not return a full commit SHA")
        return result.oid

    def _verify_inventory(self, sha, payloads, cache_dir):
        self._private()
        if not isinstance(sha, str) or not SHA.fullmatch(sha):
            raise PublishError("Full restoration revision required")
        remote = self.api.list_repo_files(self.repo_id, repo_type="dataset", revision=sha, token=self._token)
        if not isinstance(remote, list) or len(remote) != len(payloads) or set(remote) != set(payloads):
            raise PublishError("Restoration inventory mismatch")
        for name, expected in payloads.items():
            self._scan_path(name)
            payload = download_bytes(self.api, self.repo_id, name, revision=sha,
                token=self._token, cache_dir=cache_dir)
            if len(payload) != len(expected) or hashlib.sha256(payload).digest() != hashlib.sha256(expected).digest():
                raise PublishError("Restoration hash mismatch")
            scan_secret_payload(payload, name, self.secret_values(), schema_version=self._journal_schema(payload, name))

    def verify(self, commit_sha: str, manifest: dict, cache_dir: Path, *,
               previous_files=(), evidence_store=None) -> VerificationReport:
        m = validate_manifest(manifest)
        from ..storage.parquet import schema_for
        schema_for(m["data_schema_version"])
        if not isinstance(commit_sha, str) or not SHA.fullmatch(commit_sha):
            raise PublishError("Verification requires a full commit SHA")
        refs = {(r["repo_id"], r["commit_sha"]) for r in m["hugging_face_revisions"]}
        if (self.repo_id, commit_sha) not in refs:
            raise PublishError("Revision is not bound to this manifest")
        checked, errors = [], []
        inventory = {}
        try:
            self._private()
        except Exception:
            return VerificationReport(manifest_digest(m), (), (), ("privacy_check_failed",))
        retained = {}
        remote = []
        tree_enumerated = False
        try:
            current_paths = {entry["path"] for entry in m["files"]}
            # Paths alone are not evidence: require complete trusted file entries.
            for entry in previous_files:
                if (not isinstance(entry, dict) or set(entry) not in ({"path", "sha256", "rows"}, {"path", "sha256", "rows", "data_schema_version"})
                        or not isinstance(entry["sha256"], str)
                        or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
                        or type(entry["rows"]) is not int or entry["rows"] < 0):
                    raise PublishError("Retained files require manifest evidence")
                name = relative_path(entry["path"])
                if name not in current_paths:
                    if name in retained and retained[name] != [entry]:
                        raise PublishError("Ambiguous retained path ownership")
                    retained[name] = [entry]
            allowed = current_paths | set(retained) | {".gitattributes"}
            remote = self.api.list_repo_files(self.repo_id, repo_type="dataset",
                                              revision=commit_sha, token=self._token)
            if not isinstance(remote, list):
                raise PublishError("Complete remote inventory required")
            tree_enumerated = True
            if any(relative_path(name) not in allowed for name in remote):
                errors.append("unexpected_remote_path")
        except Exception:
            errors.append("remote_tree_verification_failed")
            remote = []
        # Bind both presence and bytes: disappearance after an initial baseline
        # is also a change. Never accept attributes without durable repo evidence.
        try:
            if not tree_enumerated:
                raise PublishError("Tree enumeration required before attribute binding")
            digest = None
            if ".gitattributes" in remote:
                payload = download_bytes(self.api, self.repo_id, ".gitattributes",
                                                     revision=commit_sha, cache_dir=cache_dir,
                                                     token=self._token)
                scan_secret_payload(payload, ".gitattributes", self.secret_values())
                digest = hashlib.sha256(payload).hexdigest()
                inventory[".gitattributes"] = (digest, m["data_schema_version"])
            if evidence_store is None:
                if digest is not None:
                    raise PublishError("Durable repository evidence required")
            else:
                evidence_store.bind_repository_attributes(self.repo_id, digest)
        except Exception:
            errors.append("gitattributes_verification_failed")
        # Required owner paths must be present, not merely verified if listed.
        for name in sorted(retained):
            if name not in remote:
                errors.append('retained_file_missing')
                continue
            try:
                payload = download_bytes(self.api, self.repo_id, name,
                                                     revision=commit_sha, cache_dir=cache_dir,
                                                     token=self._token)
                digest = hashlib.sha256(payload).hexdigest()
                owner = next((e for e in retained[name] if e['sha256'] == digest), None)
                if owner is None:
                    errors.append('retained_file_mismatch')
                    continue
                scan_secret_payload(payload, name, self.secret_values(),
                                    schema_version=owner.get('data_schema_version', 'kapa-runtime-data/2'))
                rows = pq.read_metadata(pa.BufferReader(payload)).num_rows if name.lower().endswith(".parquet") else 0
                if not any(entry["sha256"] == digest and entry["rows"] == rows for entry in retained[name]):
                    errors.append("retained_file_mismatch")
                else:
                    inventory[name] = (digest, retained[name][0].get("data_schema_version", "kapa-runtime-data/2"))
            except Exception:
                errors.append("retained_file_verification_failed")
        for entry in m["files"]:
            try:
                payload = download_bytes(self.api, self.repo_id, entry["path"],
                                                     revision=commit_sha, cache_dir=cache_dir,
                                                     token=self._token)
                if hashlib.sha256(payload).hexdigest() != entry["sha256"]:
                    errors.append("hash_mismatch")
                    continue
                scan_secret_payload(payload, entry["path"], self.secret_values(),
                                    schema_version=m["data_schema_version"])
                if entry["path"].lower().endswith(".parquet") and pq.read_metadata(pa.BufferReader(payload)).num_rows != entry["rows"]:
                    errors.append("row_count_mismatch")
                    continue
                checked.append(entry["path"])
                inventory[entry["path"]] = (entry["sha256"], m["data_schema_version"])
            except Exception:
                errors.append("file_verification_failed")
        if not errors and set(inventory) != set(remote):
            errors.append("incomplete_verified_inventory")
        return VerificationReport(manifest_digest(m), ((self.repo_id, commit_sha),),
                                  tuple(checked), tuple(errors),
                                  tuple((name, digest, version) for name, (digest, version) in sorted(inventory.items())))
