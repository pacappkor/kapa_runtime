# SPDX-License-Identifier: GPL-3.0-only
from datetime import date
from decimal import Decimal

import pytest

from kapa_runtime.storage.query import TransactionQuery
from test_parquet import record, write


@pytest.fixture
def snapshot(tmp_path):
    write([record("3", cancellation_status="O"), record("2", price_krw=200000000),
           record("1"), record("4", contract_date=None, area_m2=None, price_krw=None)], tmp_path)
    write([record("rent", transaction_type="rent", price_krw=None, deposit_krw=50000000,
                  monthly_rent_krw=700000)], tmp_path, "rent")
    return tmp_path


def test_default_cancelled_and_coverage(snapshot):
    result = TransactionQuery(snapshot).search({})
    assert result["total"] == 4
    assert result["excluded"] == {"count": 1, "reason": "cancelled"}
    assert result["coverage"]["lawd_codes"] == ["11110"]
    assert result["coverage"]["months"] == ["2026-10"]
    assert result["warnings"]
    assert TransactionQuery(snapshot).search({"include_cancelled": True})["total"] == 5


def test_filters_and_literal_contains(snapshot):
    query = TransactionQuery(snapshot)
    assert query.search({"price_krw_min": 150000000})["total"] == 1
    assert query.search({"area_m2_min": Decimal("84.1234"), "area_m2_max": "85"})["total"] == 3
    assert query.search({"contract_date_from": "2026-10-01", "contract_date_to": date(2026, 10, 2)})["total"] == 3
    assert query.search({"transaction_type": "rent"})["rows"][0]["monthly_rent_krw"] == 700000
    assert query.search({"building_name_contains": "' OR TRUE --"})["total"] == 0
    assert query.search({"building_name_contains": "%"})["total"] == 0
    assert query.search({"lawd_code": "11110' OR TRUE --"})["total"] == 0
    with pytest.raises(ValueError):
        query.search({"price_krw; DROP TABLE transactions": 1})


def test_pagination_binding_and_tampering(snapshot):
    query = TransactionQuery(snapshot)
    first = query.search({}, limit=1)
    assert first["rows"][0]["record_version_id"] == "1"
    cursor = first["next_cursor"]
    second = query.search({}, cursor=cursor, limit=1)
    assert second["rows"][0]["record_version_id"] == "2"
    for invalid in (cursor[:-4] + "AAAA", "not-a-cursor", "", 42):
        with pytest.raises(ValueError):
            query.search({}, cursor=invalid)
    with pytest.raises(ValueError):
        query.search({"transaction_type": "rent"}, cursor=cursor)
    resumed = TransactionQuery(snapshot).search({}, cursor=cursor, limit=1)
    assert resumed["rows"][0]["record_version_id"] == "2"
    write([record("changed")], snapshot)
    with pytest.raises(ValueError, match="snapshot changed"):
        query.search({}, cursor=cursor)


@pytest.mark.parametrize("filters", [{"include_cancelled": "yes"}, {"price_krw_min": True},
                                     {"area_m2_min": "NaN"}, {"contract_date_from": "bad"}])
def test_invalid_filters(snapshot, filters):
    with pytest.raises(ValueError):
        TransactionQuery(snapshot).search(filters)


def test_empty_and_bad_limit(tmp_path):
    query = TransactionQuery(tmp_path)
    result = query.search({})
    assert result["total"] == 0
    assert result["coverage"]["months"] == []
    for limit in (0, 1001, True):
        with pytest.raises(ValueError):
            query.search({}, limit=limit)


def test_link_escape_rejected(snapshot, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    write([record()], outside)
    link = snapshot / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises(ValueError):
        TransactionQuery(snapshot).search({})
