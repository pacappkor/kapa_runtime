# SPDX-License-Identifier: GPL-3.0-only
"""Shared data contracts (design/03_DATA_CONTRACTS.md).

All modules exchange these types. Change only with orchestrator coordination.
Money is integer KRW; areas are Decimal square metres; IDs and region codes are str.
Missing source values stay None (never 0). Timestamps are timezone-aware.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

SCHEMA_VERSION = "kapa-runtime-data/2"


class ObservationStatus(StrEnum):
    SUCCESS = "success"   # every expected page received, valid envelopes, count > 0
    ZERO = "zero"         # valid normal envelope that explicitly reports zero rows
    PARTIAL = "partial"   # some pages missing/invalid; never usable as full coverage
    FAILURE = "failure"   # nothing usable


class MatchStatus(StrEnum):
    EXACT = "exact"
    CANDIDATE = "candidate"
    UNRESOLVED = "unresolved"


class ReleaseState(StrEnum):
    DRAFT = "draft"
    COLLECTING = "collecting"
    COLLECTED = "collected"
    NORMALIZED = "normalized"
    VALIDATED = "validated"
    UPLOADED_CANDIDATE = "uploaded-candidate"
    PUBLISHED = "published"      # private release ready; never "public"
    ACTIVE = "active"
    FAILED = "failed"
    PARTIAL = "partial"
    QUARANTINED = "quarantined"


@dataclass(frozen=True)
class Observation:
    """One bounded collection run against one allowed endpoint (key-free)."""
    observation_id: str
    run_id: str
    provider: str
    service_id: str
    dataset_id: str
    endpoint_id: str
    request: dict[str, str]          # sanitized typed arguments only, never the key
    started_at: datetime
    fetched_at: datetime
    status: ObservationStatus
    pages_expected: int | None
    pages_received: int
    count: int | None                # provider totalCount when supplied, else None
    response_schema: str
    parser_version: str
    sanitized_payload_sha256: str    # sha256 of canonical sanitized envelope bytes
    source_reference_date: date | None = None
    published_at: datetime | None = None
    warnings: tuple[str, ...] = ()
    coverage: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TransactionRecord:
    """Normalized sale or lease record. Lease-only fields are None for sales."""
    observation_id: str
    record_version_id: str           # sha256 fingerprint of normalized fields + observation_id
    fingerprint: str                 # sha256 of normalized identifying fields (no observation_id)
    asset_type: str                  # "apartment", ...
    transaction_type: str            # "sale" | "rent"
    contract_date: date | None
    lawd_code: str                   # 5-digit sigungu query code (not full bjdong code)
    address_original: str | None     # umdNm + jibun as provided
    address_visibility: str          # "partial" | "full" | "none"
    building_name: str | None
    area_type: str                   # "exclusive" for excluUseAr
    area_m2: Decimal | None
    source_price: str | None         # raw string as provided, e.g. "120,000"
    source_price_unit: str | None    # "10k-KRW" for MOLIT RTMS
    price_krw: int | None
    floor: int | None
    build_year: int | None
    cancellation_status: str | None  # raw cdealType ("O" etc.) or None
    cancellation_date: date | None
    first_seen_at: datetime
    last_seen_at: datetime
    source_payload_ref: str          # "<observation_id>#page=<n>&item=<i>"
    provider_record_id: str | None = None
    match_status: MatchStatus = MatchStatus.UNRESOLVED
    deposit_krw: int | None = None
    monthly_rent_krw: int | None = None
    contract_term: str | None = None
    new_or_renewal: str | None = None
    previous_deposit_krw: int | None = None
    previous_monthly_rent_krw: int | None = None
    extra: dict[str, str | None] = field(default_factory=dict)  # registry-allowlisted provider fields only
