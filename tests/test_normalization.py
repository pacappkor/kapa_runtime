# SPDX-License-Identifier: GPL-3.0-only
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from kapa_runtime.adapters.molit_rtms import collect
from kapa_runtime.normalization.rtms import NormalizationError, normalize

FIXTURES = Path(__file__).parent / "fixtures" / "rtms"


class FakeSecret:
    def reveal(self):
        return "TESTKEY-not-real"


def result(name="sale.xml"):
    spec = SimpleNamespace(host="apis.data.go.kr", path="/1613000/RTMSDataSvcAptRent/getRTMSDataSvcAptRent" if name == "rent.xml" else "/1613000/RTMSDataSvcAptTrade/getRTMSDataSvcAptTrade",
                           method="GET", service_id="molit-apartment-rent" if name == "rent.xml" else "molit-apartment-sale", dataset_id="15126469", provider="MOLIT", page_size_max=1000)
    payload = (FIXTURES / name).read_bytes()
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=payload))) as client:
        return collect(spec, FakeSecret(), lawd_code="11110", deal_ymd="202510", client=client, run_id="test", page_size=1)


def test_sale_units_blanks_decimal():
    record = normalize(result(), transaction_type="sale")[0]
    assert record.price_krw == 1_234_560_000
    assert record.source_price == "123,456" and record.source_price_unit == "10k-KRW"
    assert record.area_m2 == Decimal("81.2340") and isinstance(record.area_m2, Decimal)
    assert record.cancellation_status is None and record.cancellation_date is None
    assert record.contract_date == date(2025, 10, 7)
    assert record.floor == 9 and record.build_year == 2019
    assert record.extra["rgstDate"] is None
    assert record.address_visibility == "partial" and record.match_status == "unresolved"
    assert record.deposit_krw is None


def test_rent_separate_values():
    record = normalize(result("rent.xml"), transaction_type="rent")[0]
    assert record.price_krw is None and record.source_price is None
    assert record.deposit_krw == 200_000_000 and record.monthly_rent_krw == 750_000
    assert record.previous_deposit_krw == 190_000_000 and record.previous_monthly_rent_krw == 700_000
    assert record.new_or_renewal == "갱신" and record.contract_term == "25.10~27.10"
    assert record.provider_record_id == "11110-SYNTHETIC"
    assert record.extra["useRRRight"] is None and "roadnm" in record.extra


def test_cancelled():
    record = normalize(result("cancelled.xml"), transaction_type="sale")[0]
    assert record.cancellation_status == "O" and record.cancellation_date == date(2025, 11, 3)


def test_malformed_number_fixture():
    with pytest.raises(NormalizationError):
        normalize(result("malformed-number.xml"), transaction_type="sale")


@pytest.mark.parametrize("field,value", [("dealAmount", "NaN"), ("dealAmount", "1.5"), ("excluUseAr", "Infinity"), ("floor", "3.0"), ("buildYear", "bad"), ("dealDay", "32"), ("cdealDay", "25.02.30")])
def test_malformed_values(field, value):
    collected = result()
    collected.pages[0].items[0][field] = value
    with pytest.raises(NormalizationError):
        normalize(collected, transaction_type="sale")


def test_null_not_zero_and_unknown_fields():
    collected = result("rent.xml")
    collected.pages[0].items[0].update(monthlyRent="0", preDeposit=" ", futureField=" preserved ")
    record = normalize(collected, transaction_type="rent")[0]
    assert record.monthly_rent_krw == 0 and record.previous_deposit_krw is None
    assert "futureField" not in record.extra


def test_fingerprints_deterministic_without_deduplication():
    collected = result()
    first = normalize(collected, transaction_type="sale")[0]
    assert normalize(collected, transaction_type="sale")[0] == first
    collected.pages[0].items[0]["excluUseAr"] = "81.234"
    assert normalize(collected, transaction_type="sale")[0].fingerprint == first.fingerprint
    other = replace(collected, observation=replace(collected.observation, observation_id="another"))
    second = normalize(other, transaction_type="sale")[0]
    assert first.fingerprint == second.fingerprint
    assert first.record_version_id != second.record_version_id
    collected.pages[0].items.append(dict(collected.pages[0].items[0]))
    rows = normalize(collected, transaction_type="sale")
    assert len(rows) == 2 and rows[0].fingerprint == rows[1].fingerprint
    assert rows[0].source_payload_ref != rows[1].source_payload_ref


def test_zero():
    assert normalize(result("zero.xml"), transaction_type="sale") == []
