# SPDX-License-Identifier: GPL-3.0-only
"""Allowlisted unit mapping; fingerprints are candidates, not deduplication IDs."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
import hashlib
import json
import re

from kapa_runtime.adapters.molit_rtms import CollectionResult
from kapa_runtime.models import TransactionRecord
from kapa_runtime.registry import ServiceRegistry


class NormalizationError(ValueError):
    """Malformed source values (messages intentionally exclude source text)."""


def _int(value: str | None, *, money: bool = False) -> int | None:
    if value is None:
        return None
    pattern = r"(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)" if money else r"-?[0-9]+"
    if re.fullmatch(pattern, value) is None:
        raise NormalizationError("invalid_integer")
    return int(value.replace(",", "")) * (10000 if money else 1)


def _area(value: str | None) -> Decimal | None:
    if value is None:
        return None
    if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value) is None:
        raise NormalizationError("invalid_area")
    return Decimal(value)


def _contract_date(row):
    values = [_int(row.get(k)) for k in ("dealYear", "dealMonth", "dealDay")]
    if all(v is None for v in values):
        return None
    if any(v is None for v in values):
        raise NormalizationError("incomplete_contract_date")
    try:
        return date(*values)
    except ValueError:
        raise NormalizationError("invalid_contract_date") from None


def _cancel_date(value):
    if value is None:
        return None
    if re.fullmatch(r"[0-9]{2}\.[0-9]{2}\.[0-9]{2}", value) is None:
        raise NormalizationError("invalid_cancellation_date")
    year, month, day = map(int, value.split("."))
    try:
        # RTMS's two-digit dates are explicitly interpreted as 2000–2099.
        return date(2000 + year, month, day)
    except ValueError:
        raise NormalizationError("invalid_cancellation_date") from None


def _hash(values):
    def encode(value):
        if isinstance(value, Decimal):
            return format(value.normalize(), "f")
        if isinstance(value, date):
            return value.isoformat()
        raise TypeError("unsupported fingerprint value")
    return hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), default=encode).encode()).hexdigest()


def normalize(result: CollectionResult, *, asset_type="apartment", transaction_type, warnings=None, data_schema_version="kapa-runtime-data/2") -> list[TransactionRecord]:
    if transaction_type not in {"sale", "rent"}:
        raise NormalizationError("unsupported_transaction_type")
    mapped = {"aptSeq", "dealYear", "dealMonth", "dealDay", "umdNm", "jibun", "aptNm",
              "excluUseAr", "dealAmount", "floor", "buildYear", "cdealType", "cdealDay",
              "deposit", "monthlyRent", "preDeposit", "preMonthlyRent", "contractTerm", "contractType"}
    records = []
    obs = result.observation
    from kapa_runtime.storage.parquet import schema_for
    schema_for(data_schema_version)
    legacy = data_schema_version == "kapa-runtime-data/1"
    allowed = set(ServiceRegistry.load().get(obs.service_id).normalized_extra_fields)
    dropped = 0
    for page in result.pages:
        for index, raw in enumerate(page.items, 1):
            row = {k: v.strip() or None if v is not None else None for k, v in raw.items()}
            dropped += sum(k not in mapped and k not in allowed for k in row)
            address = " ".join(v for v in (row.get("umdNm"), row.get("jibun")) if v) or None
            values = dict(
                asset_type=asset_type, transaction_type=transaction_type,
                contract_date=_contract_date(row), lawd_code=obs.request["LAWD_CD"],
                address_original=address, address_visibility="partial", building_name=row.get("aptNm"),
                area_type="exclusive", area_m2=_area(row.get("excluUseAr")),
                source_price=row.get("dealAmount") if transaction_type == "sale" else None,
                source_price_unit="10k-KRW", price_krw=_int(row.get("dealAmount"), money=True) if transaction_type == "sale" else None,
                floor=_int(row.get("floor")), build_year=_int(row.get("buildYear")),
                cancellation_status=row.get("cdealType"), cancellation_date=_cancel_date(row.get("cdealDay")),
                provider_record_id=row.get("aptSeq"),
                deposit_krw=_int(row.get("deposit"), money=True) if transaction_type == "rent" else None,
                monthly_rent_krw=_int(row.get("monthlyRent"), money=True) if transaction_type == "rent" else None,
                previous_deposit_krw=_int(row.get("preDeposit"), money=True) if transaction_type == "rent" else None,
                previous_monthly_rent_krw=_int(row.get("preMonthlyRent"), money=True) if transaction_type == "rent" else None,
                contract_term=row.get("contractTerm"), new_or_renewal=row.get("contractType"),
                extra={k: v for k, v in row.items() if k not in mapped and (legacy or k in allowed)},
            )
            # Original monetary strings also remain in the sanitized source page.
            identity = {k: v for k, v in values.items() if k not in {"extra", "source_price"}}
            fingerprint = _hash(identity)
            version = _hash({"observation_id": obs.observation_id, "values": values})
            records.append(TransactionRecord(
                **values, observation_id=obs.observation_id, fingerprint=fingerprint,
                record_version_id=version, first_seen_at=obs.fetched_at, last_seen_at=obs.fetched_at,
                source_payload_ref=f"{obs.observation_id}#page={page.page_no}&item={index}",
            ))
    if dropped and warnings is not None:
        warnings.append(f"unknown_provider_fields_dropped:{dropped}")
    return records
