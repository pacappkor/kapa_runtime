# SPDX-License-Identifier: GPL-3.0-only
from datetime import date, datetime, timedelta, timezone
from importlib.resources import files
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys

import pytest

from kapa_runtime.planning import Budget, ServiceRange, plan_day, refresh_due_on

TODAY = date(2026, 11, 12)


def test_budget_and_cap():
    budget = Budget()
    assert budget.pair_capacity == 9000 // 150 == 60
    assert [budget.lane_capacity(p) for p in (20, 5, 75)] == [12, 3, 45]
    plan = plan_day(["11110", "26110"], today=TODAY, coverage={})
    assert len(plan.items) == 60
    assert plan.reserved_requests_per_service == 9000
    assert plan.refresh_share == 12
    assert plan.refresh_due == 26
    assert plan.refresh_sla_shortfall == 14
    assert len({item.pair for item in plan.items}) == 60
    # Empty audit donates its three unused slots to backfill.
    assert sum(item.lane == "backfill" for item in plan.items) == 48
    asymmetric = plan_day(["11110"], today=TODAY, coverage={},
                          budget=Budget(sale_requests=9000, rent_requests=1499))
    assert len(asymmetric.items) == 9
    assert asymmetric.reserved_requests_per_service == 1350
    assert len(plan_day(["11110"], today=TODAY, coverage={},
                        budget=Budget(batch_cap=4)).items) == 4


def test_order_rotation_and_determinism():
    codes = ["26110", "11140", "11110"]
    coverage = {(code, "201101"): date(2020, 1, 1) for code in codes}
    result = plan_day(codes, today=TODAY, coverage=coverage)
    reordered = plan_day(reversed(codes), today=TODAY, coverage=dict(reversed(list(coverage.items()))))
    assert result == reordered
    lanes = [item.lane for item in result.items]
    assert lanes == sorted(lanes, key=["refresh", "audit", "backfill"].index)
    first = [item for item in result.items if item.deal_ymd == "202611" and item.lane == "refresh"]
    assert [item.lawd_code for item in first] == sorted(codes)
    # Without successful observations there is no cursor advancement.
    tomorrow = plan_day(codes, today=date(2026, 11, 13), coverage=coverage)
    assert tomorrow.items == result.items
    backfill = [item.pair for item in result.items if item.lane == "backfill"]
    assert backfill == sorted(backfill, key=lambda p: (-int(p[1]), p[0]))


def test_audit_rotates_and_skips_todays_observations():
    coverage = {("11110", f"2011{month:02d}"): date(2020, 1, 1) for month in range(1, 13)}
    first = plan_day(["11110"], today=TODAY, coverage=coverage)
    audits = [item.pair for item in first.items if item.lane == "audit"]
    assert len(audits) == 3
    coverage.update({pair: TODAY for pair in audits})
    second = plan_day(["11110"], today=date(2026, 11, 13), coverage=coverage)
    assert not set(audits) & {item.pair for item in second.items if item.lane == "audit"}
    assert not set(audits) & {i.pair for i in plan_day(["11110"], today=TODAY, coverage=coverage).items}


@pytest.mark.parametrize("initial_observation", [None, date(2026, 10, 1)])
def test_refresh_full_registry_rotation(initial_observation):
    table = json.loads(files("kapa_runtime.registry").joinpath("lawd_codes.json").read_text(encoding="utf-8"))
    codes = [row["code"] for row in table["codes"]]
    assert len(codes) == 269
    start = date(2026, 11, 1)
    coverage = {} if initial_observation is None else {
        (code, "202611"): initial_observation for code in codes}
    seen = set()
    for offset in range((len(codes) + 11) // 12):
        day = start + timedelta(days=offset)
        plan = plan_day(codes, today=day, coverage=coverage)
        assert plan == plan_day(reversed(codes), today=day,
                                coverage=dict(reversed(list(coverage.items()))))
        refresh = {item.pair for item in plan.items if item.lane == "refresh"}
        assert len(refresh) == 12
        assert all(ym == "202611" for _, ym in refresh)
        assert len(refresh - seen) == min(12, len(codes) - len(seen))
        seen.update(refresh)
        coverage.update({pair: day for pair in refresh})
    assert seen == {(code, "202611") for code in codes}


def test_audit_full_rotation_with_varying_budget():
    codes = [f"{11001 + n:05d}" for n in range(269)]
    coverage = {(code, "201101"): date(2020, 1, 1) for code in codes}
    seen = set()
    offset = 0
    while len(seen) < len(codes):
        day = TODAY + timedelta(days=offset)
        budget = Budget(sale_requests=9000 if offset % 2 else 6000)
        plan = plan_day(codes, today=day, coverage=coverage, budget=budget)
        audits = {item.pair for item in plan.items if item.lane == "audit"}
        assert len(audits - seen) == min(budget.lane_capacity(5), len(codes) - len(seen))
        seen.update(audits)
        coverage.update({pair: day for pair in audits})
        offset += 1
    assert seen == {(code, "201101") for code in codes}


def test_refresh_missing_then_oldest_with_varying_budget():
    codes = [f"{11001 + n:05d}" for n in range(30)]
    coverage = {(code, "202611"): date(2026, 10, 1) for code in codes[:20]}
    seen = set()
    for offset, requests in enumerate([3000, 6000, 9000, 9000]):
        day = date(2026, 11, 1) + timedelta(days=offset)
        plan = plan_day(codes, today=day, coverage=coverage,
                        budget=Budget(sale_requests=requests))
        refresh = [item.pair for item in plan.items if item.lane == "refresh"]
        if offset == 0:
            assert refresh == [(code, "202611") for code in codes[20:24]]
        assert len(set(refresh) - seen) == min(len(refresh), len(codes) - len(seen))
        seen.update(refresh)
        coverage.update({pair: day for pair in refresh})
    assert len(seen) == len(codes)


@pytest.mark.parametrize("ym,observed,today,expected", [
    ("202701", date(2027, 1, 1), date(2027, 1, 1), date(2027, 1, 2)),
    ("202612", date(2026, 12, 31), date(2027, 1, 1), date(2027, 1, 7)),
    ("202610", date(2026, 12, 27), date(2027, 1, 1), date(2027, 1, 3)),
    ("202609", date(2026, 12, 31), date(2027, 1, 1), date(2027, 1, 31)),
    ("202610", date(2027, 1, 31), date(2027, 2, 1), date(2027, 2, 28)),
    ("202310", date(2024, 1, 31), date(2024, 2, 1), date(2024, 2, 29)),
    ("202601", None, date(2027, 1, 1), date(2027, 1, 1)),
    ("202512", None, date(2027, 1, 1), None),
    ("202702", None, date(2027, 1, 1), None),
])
def test_refresh_boundaries(ym, observed, today, expected):
    assert refresh_due_on(ym, observed, today) == expected


def test_weekly_due_inclusive_and_seoul_timezone():
    coverage = {("11110", "202611"): date(2026, 12, 25)}
    before = plan_day(["11110"], today=date(2026, 12, 31), coverage=coverage)
    due = plan_day(["11110"], today=datetime(2026, 12, 31, 15, tzinfo=timezone.utc), coverage=coverage)
    assert ("11110", "202611") not in {i.pair for i in before.items if i.lane == "refresh"}
    assert ("11110", "202611") in {i.pair for i in due.items if i.lane == "refresh"}
    assert due.day == date(2027, 1, 1)


def test_seoul_full_history_before_nationwide_transition():
    budget = Budget(refresh_percent=0, audit_percent=0, backfill_percent=100)
    result = plan_day(["26110", "11140", "11110"], today=date(2011, 2, 1), coverage={}, budget=budget)
    assert [i.pair for i in result.items] == [
        ("11110", "201102"), ("11140", "201102"),
        ("11110", "201101"), ("11140", "201101"),
        ("26110", "201102"), ("26110", "201101"),
    ]
    covered = {item.pair: date(2011, 2, 1) for item in result.items[:4]}
    transition = plan_day(["11140", "26110", "11110"], today=date(2011, 2, 1), coverage=covered, budget=budget)
    assert [i.pair for i in transition.items] == [("26110", "201102"), ("26110", "201101")]


def test_overlapping_period_and_empty_intersection():
    result = plan_day(["11110"], today=TODAY, coverage={}, sale=ServiceRange("200601", "201103"))
    assert result.overlap_start == "201101"
    assert result.overlap_end == "201103"
    assert {item.deal_ymd for item in result.items} == {"201101", "201102", "201103"}
    assert not plan_day(["11110"], today=TODAY, coverage={}, sale=ServiceRange("200601", "201012")).items
    assert not plan_day(["11110"], today=date(2010, 12, 31), coverage={}).items


def test_empty_and_complete_coverage():
    assert not plan_day([], today=TODAY, coverage={}).items
    assert not plan_day([{"code": "11110", "active": False}], today=TODAY, coverage={}).items
    kwargs = dict(lawd_codes=["11110"], today=TODAY, sale=ServiceRange("202610"), rent=ServiceRange("202610"))
    empty = plan_day(**kwargs, coverage={})
    assert {item.pair for item in empty.items} == {("11110", "202610"), ("11110", "202611")}
    complete = plan_day(**kwargs, coverage={item.pair: TODAY for item in empty.items})
    assert not complete.items
    assert complete.refresh_due == 0
    assert not plan_day(**kwargs, coverage={}, budget=Budget(sale_requests=0)).items


@pytest.mark.parametrize("kwargs", [
    {"requests_per_pair": 151}, {"requests_per_pair": 4}, {"batch_cap": 61},
    {"refresh_percent": 21}, {"rent_requests": -1}, {"sale_requests": True},
])
def test_invalid_budget(kwargs):
    with pytest.raises(ValueError):
        Budget(**kwargs)


def test_invalid_inputs():
    with pytest.raises(ValueError):
        plan_day(["11000"], today=TODAY, coverage={})
    with pytest.raises(ValueError):
        plan_day(["11110"], today=TODAY, coverage={}, sale=ServiceRange("202613"))
    with pytest.raises(ValueError):
        plan_day(["11110"], today=datetime(2026, 11, 12), coverage={})
    with pytest.raises(ValueError):
        plan_day(["11110"], today=TODAY, coverage={("11110", "202611"): date(2026, 11, 13)})


def test_reference_schema_and_provenance():
    registry = files("kapa_runtime.registry")
    table = json.loads(registry.joinpath("lawd_codes.json").read_text(encoding="utf-8"))
    provenance = json.loads(registry.joinpath("lawd_codes.provenance.json").read_text(encoding="utf-8"))
    assert table["license"] == provenance["license"] == "SPDX-License-Identifier: GPL-3.0-only"
    assert table["schema"] == "kapa-runtime-lawd/1"
    rows = table["codes"]
    assert len(rows) == provenance["count"] > 200
    assert [r["code"] for r in rows] == sorted({r["code"] for r in rows})
    for row in rows:
        assert set(row) == {"code", "name", "sido", "active"}
        assert re.fullmatch(r"[0-9]{5}", row["code"]) and not row["code"].endswith("000")
        assert row["active"] is True and row["name"].startswith(row["sido"])
    assert sum(r["code"].startswith("11") for r in rows) == 25
    assert "36110" in {r["code"] for r in rows}
    assert provenance["source_url"].startswith("https://www.code.go.kr/")
    assert provenance["download_date"] == "2026-11-12"
    assert re.fullmatch(r"[0-9a-f]{64}", provenance["raw_sha256"])
    assert re.fullmatch(r"[0-9a-f]{64}", provenance["payload_sha256"])
    assert provenance["historical_codes"] == "unresolved"


def test_generator_active_prefix_rule_and_determinism():
    path = Path(__file__).resolve().parents[1] / "tools" / "build_lawd_codes.py"
    spec = importlib.util.spec_from_file_location("build_lawd_codes", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    raw = ("법정동코드\t법정동명\t폐지여부\n"
           "1100000000\t서울특별시\t존재\n"
           "1111000000\t서울특별시 종로구\t존재\n"
           "1111010100\t서울특별시 종로구 청운동\t존재\n"
           "1114000000\t서울특별시 중구\t폐지\n"
           "3611000000\t세종특별자치시\t존재\n").encode("cp949")
    table, provenance = module.generate(raw, "2026-11-12")
    assert [r["code"] for r in table["codes"]] == ["11110", "36110"]
    assert table["codes"][0]["name"] == "서울특별시 종로구"
    assert provenance["count"] == 2
    assert (table, provenance) == module.generate(raw, "2026-11-12")
    with pytest.raises(ValueError):
        module.generate(b"wrong columns", "2026-11-12")
    for invalid in ["20261112", "2026-1-12", "2026-02-30", "2026-W46-4", "2026-11-12T00:00:00"]:
        with pytest.raises(ValueError):
            module.generate(raw, invalid)


def test_generator_cli_requires_valid_download_date(tmp_path):
    script = Path(__file__).resolve().parents[1] / "tools" / "build_lawd_codes.py"
    raw = tmp_path / "raw.tsv"
    raw.write_text("법정동코드\t법정동명\t폐지여부\n1111000000\t서울특별시 종로구\t존재\n", encoding="utf-8")
    output = tmp_path / "output"
    command = [sys.executable, "-I", "-B", str(script), str(raw), "--output-dir", str(output)]
    missing = subprocess.run(command, capture_output=True, text=True)
    assert missing.returncode == 2 and "--download-date" in missing.stderr
    for invalid in ["20261112", "2026-02-30"]:
        result = subprocess.run(command + ["--download-date", invalid], capture_output=True, text=True)
        assert result.returncode == 2
    assert not output.exists()
    valid = command + ["--download-date", "2026-11-12"]
    subprocess.run(valid, check=True, capture_output=True)
    first = {path.name: path.read_bytes() for path in output.iterdir()}
    subprocess.run(valid, check=True, capture_output=True)
    assert first == {path.name: path.read_bytes() for path in output.iterdir()}
    assert json.loads(first["lawd_codes.provenance.json"])["download_date"] == "2026-11-12"
