# SPDX-License-Identifier: GPL-3.0-only
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib

import pyarrow.parquet as pq
import pytest

from kapa_runtime.models import SCHEMA_VERSION, TransactionRecord
from kapa_runtime.storage.parquet import read_partition, write_partition


def record(identifier="1", **changes):
    stamp = datetime(2026, 10, 1, tzinfo=timezone.utc)
    row = TransactionRecord(
        observation_id="obs", record_version_id=identifier, fingerprint="fp",
        asset_type="apartment", transaction_type="sale", contract_date=date(2026, 10, 1),
        lawd_code="11110", address_original=None, address_visibility="partial",
        building_name="Synthetic apartment", area_type="exclusive", area_m2=Decimal("84.1234"),
        source_price="12,000", source_price_unit="10k-KRW", price_krw=120000000,
        floor=None, build_year=None, cancellation_status=None, cancellation_date=None,
        first_seen_at=stamp, last_seen_at=stamp, source_payload_ref="obs#page=1&item=1",
        extra={"unknown": None, "z": "raw", "a": "text"},
    )
    return replace(row, **changes)


def write(records, root, transaction_type="sale"):
    return write_partition(records, root, asset_type="apartment", transaction_type=transaction_type,
                           lawd_code="11110", contract_year=2026, contract_month=10)[0]


def test_round_trip_and_deterministic_bytes(tmp_path):
    records = [record("2", area_m2=None, price_krw=None, contract_date=None), record("1")]
    first = write(records, tmp_path)
    digest = hashlib.sha256(first.path.read_bytes()).hexdigest()
    second = write(list(reversed(records)), tmp_path)
    assert first.sha256 == second.sha256 == digest
    assert second.rows == 2
    assert read_partition(second.path) == list(reversed(records))
    schema = pq.ParquetFile(second.path).schema_arrow
    assert schema.metadata[b"schema_version"] == SCHEMA_VERSION.encode()
    assert str(schema.field("area_m2").type) == "decimal128(12, 4)"
    assert schema.field("first_seen_at").type.tz == "UTC"


def test_rent_and_decimal_boundary(tmp_path):
    rent = record(transaction_type="rent", price_krw=None, source_price=None,
                  deposit_krw=100000000, monthly_rent_krw=450000,
                  previous_deposit_krw=90000000, area_m2=Decimal("99999999.9999"))
    result = write([rent], tmp_path, "rent")
    assert read_partition(result.path) == [rent]


@pytest.mark.parametrize("area", ["1.00001", "100000000", "NaN", "Infinity", "-100000000"])
def test_invalid_decimal_is_not_rounded(tmp_path, area):
    with pytest.raises(ValueError):
        write([record(area_m2=Decimal(area))], tmp_path)
    assert not list(tmp_path.rglob("*.parquet"))


def test_invalid_partition_and_time(tmp_path):
    with pytest.raises(ValueError):
        write([record(lawd_code="22222")], tmp_path)
    with pytest.raises(ValueError):
        write([record(first_seen_at=datetime(2026, 10, 1))], tmp_path)
    with pytest.raises(ValueError):
        write_partition([], tmp_path, asset_type="../escape", transaction_type="sale",
                        lawd_code="11110", contract_year=2026, contract_month=10)


def test_empty_partition(tmp_path):
    result = write([], tmp_path)
    assert result.rows == 0
    assert read_partition(result.path) == []


def test_symlink_rejected(tmp_path):
    target = tmp_path / "real"
    target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises(ValueError):
        write([record()], link)
