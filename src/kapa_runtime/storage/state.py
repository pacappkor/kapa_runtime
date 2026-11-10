# SPDX-License-Identifier: GPL-3.0-only
"""Versioned single-writer execution ledger; no request URLs are persisted."""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3

from kapa_runtime.models import Observation, ReleaseState
from .parquet import _safe_path

# Terminal branches deliberately require a new release rather than skipping checks on retry.
_SEQUENCE = list(ReleaseState)[:8]
ALLOWED_TRANSITIONS = {
    state: ({_SEQUENCE[index + 1]} if index + 1 < len(_SEQUENCE) else set())
    for index, state in enumerate(_SEQUENCE)
}
for _state in _SEQUENCE[:-1]:
    ALLOWED_TRANSITIONS[_state].add(ReleaseState.FAILED)
for _state in (ReleaseState.COLLECTING,):
    ALLOWED_TRANSITIONS[_state].add(ReleaseState.PARTIAL)
for _state in (ReleaseState.COLLECTED, ReleaseState.NORMALIZED, ReleaseState.VALIDATED,
               ReleaseState.UPLOADED_CANDIDATE, ReleaseState.PUBLISHED):
    ALLOWED_TRANSITIONS[_state].add(ReleaseState.QUARANTINED)
for _state in (ReleaseState.FAILED, ReleaseState.PARTIAL, ReleaseState.QUARANTINED):
    ALLOWED_TRANSITIONS[_state] = set()

# Every named check must be an explicit True, not merely a truthy object.
REQUIRED_GUARDS = {
    ReleaseState.COLLECTING: ("approval_verified", "scope_verified", "quota_verified", "output_verified"),
    ReleaseState.COLLECTED: ("all_pages_received", "responses_valid", "zero_envelope_valid"),
    ReleaseState.NORMALIZED: ("units_valid", "dates_valid", "fields_valid", "corrections_valid"),
    ReleaseState.VALIDATED: ("rows_verified", "hashes_verified", "privacy_checked", "secrets_checked", "terms_verified", "fixtures_passed"),
    ReleaseState.UPLOADED_CANDIDATE: ("private_verified", "namespace_verified", "allowlist_verified"),
    ReleaseState.PUBLISHED: ("commit_reread", "files_verified", "hashes_verified", "schema_verified"),
    ReleaseState.ACTIVE: ("repo_revisions_verified", "manifest_atomically_replaced"),
}


def _default(value):
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, (date, datetime, Decimal)):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    raise TypeError("unsupported ledger value")


def _dump(value) -> str:
    """Reject URLs recursively, including requests nested in observations/evidence."""
    text = json.dumps(value, default=_default, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)
    if re.search(r"[a-zA-Z][a-zA-Z0-9+.-]*://", text) or re.search(r'"(?:request_)?url"\s*:', text, re.I):
        raise ValueError("request URLs cannot be stored")
    return text


_RESTORE_SEAL = object()


class _RestorationAuthority:
    """Internal, state-store issued evidence; never constructed from caller payloads."""
    def __init__(self, seal, store, release_id, digest):
        if seal is not _RESTORE_SEAL:
            raise ValueError("journal authority required")
        self._store, self._release_id, self._digest = store, release_id, digest

    def _inventory(self, repo_id):
        return self._store._restoration_inventory(self._release_id, repo_id, self._digest)


class StateStore:
    """Hold the writer lock until close (context-manager use is recommended).

    A crash leaves a lock file deliberately: operators must verify no writer is
    alive before removing a stale lock. Never silently steal another writer's lock.
    """

    def __init__(self, db_path):
        self.path = Path(db_path).absolute()
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self._connection = None
        self._lock_fd = None
        _safe_path(self.path)
        _safe_path(self.lock_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._lock_fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise RuntimeError("state writer lock is held") from exc
        try:
            os.write(self._lock_fd, str(os.getpid()).encode("ascii"))
            self._connection = sqlite3.connect(self.path, timeout=0, isolation_level=None)
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA journal_mode=DELETE")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._migrate()
        except BaseException:
            self.close()
            raise

    def _migrate(self):
        con = self._connection
        version = con.execute("PRAGMA user_version").fetchone()[0]
        if version > 2:
            raise ValueError("unsupported state schema version")
        if version == 0:
            con.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE runs (run_id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE observations (observation_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(run_id), data TEXT NOT NULL);
                CREATE TABLE releases (release_id TEXT PRIMARY KEY, state TEXT NOT NULL,
                    guard_evidence TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE idempotency (key TEXT PRIMARY KEY, service_id TEXT NOT NULL,
                    parameters TEXT NOT NULL, parser_version TEXT NOT NULL, scope TEXT NOT NULL);
                PRAGMA user_version=1;
                COMMIT;
            """)

        if version <= 1:
            con.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE repository_evidence (repo_id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE public_promotions (release_id TEXT PRIMARY KEY, data TEXT NOT NULL);
                PRAGMA user_version=2;
                COMMIT;
            """)

    def repository_evidence(self, repo_id):
        row = self._connection.execute("SELECT data FROM repository_evidence WHERE repo_id=?", (repo_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def bind_repository_attributes(self, repo_id, digest):
        existing = self.repository_evidence(repo_id)
        if existing is not None and existing["gitattributes_sha256"] != digest:
            raise ValueError("repository_attributes_changed")
        self._connection.execute("INSERT OR IGNORE INTO repository_evidence VALUES (?,?)",
                                 (repo_id, _dump({"gitattributes_sha256": digest})))

    def save_promotion(self, release_id, evidence):
        evidence = dict(evidence)
        evidence.pop("journal_sha256", None)
        evidence["release_id"] = release_id
        evidence["journal_sha256"] = hashlib.sha256(_dump(evidence).encode()).hexdigest()
        self._connection.execute("INSERT OR REPLACE INTO public_promotions VALUES (?,?)",
                                 (release_id, _dump(evidence)))

    def promotion(self, release_id):
        row = self._connection.execute("SELECT data FROM public_promotions WHERE release_id=?", (release_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def _restoration_inventory(self, release_id, repo_id, digest):
        evidence = self.promotion(release_id)
        if evidence is None:
            raise ValueError("restoration journal required")
        evidence = dict(evidence)
        recorded = evidence.pop("journal_sha256", None)
        actual = hashlib.sha256(_dump(evidence).encode()).hexdigest()
        if (recorded != digest or actual != digest or evidence.get("release_id") != release_id
                or evidence.get("repo_id") != repo_id or self.get_release(release_id) is None):
            raise ValueError("restoration journal identity or digest mismatch")
        parent = evidence.get("public_commit_sha")
        if not isinstance(parent, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", parent):
            raise ValueError("restoration journal expected head required")
        return {name: bytes.fromhex(value) for name, value in evidence["restore_payloads"].items()}, parent

    def _restoration_authority(self, release_id, repo_id):
        evidence = self.promotion(release_id)
        digest = evidence.get("journal_sha256") if evidence else None
        self._restoration_inventory(release_id, repo_id, digest)
        return _RestorationAuthority(_RESTORE_SEAL, self, release_id, digest)

    def pending_promotions(self):
        return [row[0] for row in self._connection.execute("SELECT release_id FROM public_promotions ORDER BY release_id")]

    def finish_promotion(self, release_id):
        self._connection.execute("DELETE FROM public_promotions WHERE release_id=?", (release_id,))

    def close(self):
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None
            self.lock_path.unlink()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def record_run(self, run_id: str, data: dict | None = None, **metadata):
        payload = dict(data or {})
        payload.update(metadata)
        payload["run_id"] = run_id
        encoded = _dump(payload)
        self._connection.execute("INSERT INTO runs(run_id,data) VALUES (?,?)", (run_id, encoded))

    def reserved_requests(self, service_id: str, quota_day: str) -> int:
        """Conservative daily reservations, serialized by this store's writer lock."""
        total = 0
        for row in self._connection.execute("SELECT data FROM runs"):
            data = json.loads(row[0])
            if data.get("service_id") == service_id and data.get("quota_day") == quota_day:
                total += data.get("reserved_requests", 0)
        return total

    def record_observation(self, observation: Observation):
        if not isinstance(observation, Observation):
            raise TypeError("expected Observation")
        for value in (observation.started_at, observation.fetched_at, observation.published_at):
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError("observation timestamps must be timezone-aware")
        self._connection.execute("INSERT INTO observations(observation_id,run_id,data) VALUES (?,?,?)",
                                 (observation.observation_id, observation.run_id, _dump(observation)))

    def transition_release(self, release_id, to_state, guard_evidence: dict):
        _dump(release_id)
        to_state = ReleaseState(to_state)
        if not isinstance(guard_evidence, dict):
            raise ValueError("guard evidence must be a dict")
        encoded = _dump(guard_evidence)
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self.get_release(release_id)
            if existing is None:
                if to_state != ReleaseState.DRAFT:
                    raise ValueError("new releases must start in draft")
            else:
                current = ReleaseState(existing["state"])
                if to_state not in ALLOWED_TRANSITIONS[current]:
                    raise ValueError(f"forbidden release transition: {current} -> {to_state}")
            missing = [name for name in REQUIRED_GUARDS.get(to_state, ()) if guard_evidence.get(name) is not True]
            if missing:
                raise ValueError("missing release guards: " + ", ".join(missing))
            if to_state in {ReleaseState.FAILED, ReleaseState.PARTIAL, ReleaseState.QUARANTINED}:
                if not isinstance(guard_evidence.get("reason"), str) or not guard_evidence["reason"].strip():
                    raise ValueError("branch transitions require a reason")
                if to_state == ReleaseState.PARTIAL and not guard_evidence.get("limited_scope"):
                    raise ValueError("partial releases require limited_scope evidence")
            now = datetime.now(timezone.utc).isoformat()
            history = list(existing["guard_evidence"]) if existing else []
            history.append({"state": to_state.value, "evidence": json.loads(encoded), "at": now})
            self._connection.execute("""INSERT INTO releases VALUES (?,?,?,?)
                ON CONFLICT(release_id) DO UPDATE SET state=excluded.state,
                guard_evidence=excluded.guard_evidence, updated_at=excluded.updated_at""",
                (release_id, to_state.value, _dump(history), now))
            self._connection.execute("COMMIT")
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        return self.get_release(release_id)

    def _attest_legacy(self, release_id, digest, commit_sha):
        row = self.get_release(release_id)
        if row is None or row["state"] not in ("published", "active"):
            raise ValueError("published legacy release required")
        history = row["guard_evidence"]
        published = [event for event in history if event["state"] == "published"
                     and event["evidence"].get("commit_reread") is True]
        if len(published) != 1 or published[0]["evidence"].get("published_manifest_sha256") not in (None, digest):
            raise ValueError("legacy publication evidence conflicts")
        published[0]["evidence"].update(published_manifest_sha256=digest,
            legacy_attestation={"method": "legacy-remote-attestation", "commit_sha": commit_sha,
                                "attested_at": datetime.now(timezone.utc).isoformat()})
        self._connection.execute("UPDATE releases SET guard_evidence=? WHERE release_id=?",
                                 (_dump(history), release_id))

    def update_observation_warnings(self, observation):
        self._connection.execute("UPDATE observations SET data=? WHERE observation_id=? AND run_id=?",
                                 (_dump(observation), observation.observation_id, observation.run_id))

    def _record_serving_removal(self, release_id, path, repo_id):
        row = self.get_release(release_id)
        if row is None:
            raise ValueError("serving release required")
        history = row["guard_evidence"]
        history.append({"state": row["state"], "at": datetime.now(timezone.utc).isoformat(),
                        "evidence": {"fully_served": False, "removed_path": path, "repo_id": repo_id,
                                     "reason": "retained_partition_removed"}})
        self._connection.execute("UPDATE releases SET guard_evidence=? WHERE release_id=?",
                                 (_dump(history), release_id))

    def idempotency_key(self, service_id: str, params: dict, parser_version: str, scope: str) -> str:
        """Record a stable scope key, without deduplicating observations."""
        parameters = _dump(params)
        key = hashlib.sha256(_dump([service_id, params, parser_version, scope]).encode()).hexdigest()
        self._connection.execute("INSERT OR IGNORE INTO idempotency VALUES (?,?,?,?,?)",
                                 (key, service_id, parameters, parser_version, scope))
        return key

    def get_release(self, release_id):
        row = self._connection.execute("SELECT * FROM releases WHERE release_id=?", (release_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["guard_evidence"] = json.loads(result["guard_evidence"])
        result["fully_served"] = not any(e["evidence"].get("fully_served") is False
                                         for e in result["guard_evidence"])
        return result

    def list_releases(self):
        return [self.get_release(row[0]) for row in self._connection.execute("SELECT release_id FROM releases ORDER BY release_id").fetchall()]
