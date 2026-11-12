# SPDX-License-Identifier: GPL-3.0-only
from datetime import datetime, timezone
import sqlite3

import pytest

from kapa_runtime.models import Observation, ObservationStatus, ReleaseState
from kapa_runtime.storage.state import REQUIRED_GUARDS, StateStore


def observation(**changes):
    values = dict(observation_id="o", run_id="run", provider="molit", service_id="sale",
                  dataset_id="synthetic", endpoint_id="sale", request={"LAWD_CD": "11110"},
                  started_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
                  fetched_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
                  status=ObservationStatus.ZERO, pages_expected=1, pages_received=1, count=0,
                  response_schema="test/1", parser_version="1", sanitized_payload_sha256="a" * 64)
    values.update(changes)
    return Observation(**values)


def advance(store, target=ReleaseState.ACTIVE):
    for state in list(ReleaseState)[:8]:
        store.transition_release("release", state, {key: True for key in REQUIRED_GUARDS.get(state, ())})
        if state == target:
            break


def test_full_transition_and_reopen(tmp_path):
    path = tmp_path / "state.sqlite"
    with StateStore(path) as store:
        store.record_run("run", {"service_id": "sale"})
        store.record_observation(observation())
        advance(store)
        assert store.get_release("release")["state"] == "active"
        assert len(store.get_release("release")["guard_evidence"]) == 8
        first = store.idempotency_key("sale", {"a": "1", "b": "2"}, "v1", "scope")
        assert first == store.idempotency_key("sale", {"b": "2", "a": "1"}, "v1", "scope")
        assert first != store.idempotency_key("sale", {"a": "1", "b": "2"}, "v2", "scope")
    for _ in range(2):
        with StateStore(path) as store:
            assert store.list_releases()[0]["state"] == "active"
            assert store._connection.execute("PRAGMA user_version").fetchone()[0] == 2
            assert store._connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
            assert store._connection.execute("SELECT count(*) FROM observations").fetchone()[0] == 1
            assert store._connection.execute("SELECT count(*) FROM idempotency").fetchone()[0] == 2


def test_lock_contention_and_cleanup(tmp_path):
    path = tmp_path / "state.sqlite"
    with StateStore(path):
        with pytest.raises(RuntimeError, match="lock is held"):
            StateStore(path)
        assert path.with_name("state.sqlite.lock").exists()
    assert not path.with_name("state.sqlite.lock").exists()
    with StateStore(path):
        pass


def test_guards_cannot_be_skipped_or_truthy(tmp_path):
    with StateStore(tmp_path / "state.sqlite") as store:
        with pytest.raises(ValueError):
            store.transition_release("release", "active", {})
        store.transition_release("release", "draft", {})
        for evidence in ({}, {key: "yes" for key in REQUIRED_GUARDS[ReleaseState.COLLECTING]}):
            with pytest.raises(ValueError, match="guards"):
                store.transition_release("release", "collecting", evidence)
        assert store.get_release("release")["state"] == "draft"
        with pytest.raises(ValueError):
            store.transition_release("release", "collected", {})
        assert len(store.get_release("release")["guard_evidence"]) == 1


@pytest.mark.parametrize("branch,stage,evidence", [
    ("failed", ReleaseState.DRAFT, {"reason": "synthetic failure"}),
    ("partial", ReleaseState.COLLECTING, {"reason": "missing page", "limited_scope": {"months": ["2026-10"]}}),
    ("quarantined", ReleaseState.COLLECTED, {"reason": "schema conflict"}),
])
def test_terminal_branches(tmp_path, branch, stage, evidence):
    with StateStore(tmp_path / "state.sqlite") as store:
        advance(store, stage)
        with pytest.raises(ValueError):
            store.transition_release("release", branch, {})
        store.transition_release("release", branch, evidence)
        with pytest.raises(ValueError):
            store.transition_release("release", "active", {})


def test_request_urls_never_persist(tmp_path):
    path = tmp_path / "state.sqlite"
    with StateStore(path) as store:
        for call in (
            lambda: store.record_run("run", {"request_url": "redacted"}),
            lambda: store.record_run("run", {"nested": ["https://invalid.example/path"]}),
            lambda: store.record_run("run", {"terms_basis":
                "https://apis.data.go.kr/endpoint?serviceKey=TESTKEY-not-real&LAWD_CD=11110"}),
            lambda: store.transition_release("release", "draft", {"terms_basis":
                "https://apis.data.go.kr/endpoint?serviceKey=TESTKEY-not-real"}),
            lambda: store.idempotency_key("sale", {"url": "x"}, "v1", "scope"),
        ):
            with pytest.raises(ValueError):
                call()
        store.record_run("run")
        with pytest.raises(ValueError):
            store.record_observation(observation(request={"url": "https://invalid.example"}))
        with pytest.raises(sqlite3.IntegrityError):
            store.record_observation(observation(run_id="missing"))
        assert store._connection.execute("SELECT count(*) FROM observations").fetchone()[0] == 0


def test_future_migration_refused_releases_lock(tmp_path):
    path = tmp_path / "state.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version=999")
    with pytest.raises(ValueError):
        StateStore(path)
    assert not path.with_name("state.sqlite.lock").exists()
