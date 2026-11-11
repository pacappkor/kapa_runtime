# SPDX-License-Identifier: GPL-3.0-only
"""Typed, deterministic and atomically replaced transaction partitions."""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from datetime import date, timezone
from decimal import Decimal, localcontext
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

import pyarrow as pa
import pyarrow.parquet as pq

from kapa_runtime.models import MatchStatus, SCHEMA_VERSION, TransactionRecord

_INT_FIELDS = {
    "price_krw", "floor", "build_year", "deposit_krw", "monthly_rent_krw",
    "previous_deposit_krw", "previous_monthly_rent_krw",
}
_DATE_FIELDS = {"contract_date", "cancellation_date"}
_TIME_FIELDS = {"first_seen_at", "last_seen_at"}
SCHEMA = pa.schema([
    pa.field(f.name, pa.int64() if f.name in _INT_FIELDS else
             pa.date32() if f.name in _DATE_FIELDS else
             pa.timestamp("us", tz="UTC") if f.name in _TIME_FIELDS else
             pa.decimal128(12, 4) if f.name == "area_m2" else pa.string())
    for f in fields(TransactionRecord)
], metadata={b"schema_version": SCHEMA_VERSION.encode()})


# Frozen historical contract: explicit fields, never derived from future models.
_FROZEN_FIELDS = ('observation_id', 'record_version_id', 'fingerprint', 'asset_type', 'transaction_type', 'contract_date', 'lawd_code', 'address_original', 'address_visibility', 'building_name', 'area_type', 'area_m2', 'source_price', 'source_price_unit', 'price_krw', 'floor', 'build_year', 'cancellation_status', 'cancellation_date', 'first_seen_at', 'last_seen_at', 'source_payload_ref', 'provider_record_id', 'match_status', 'deposit_krw', 'monthly_rent_krw', 'contract_term', 'new_or_renewal', 'previous_deposit_krw', 'previous_monthly_rent_krw', 'extra')
_FROZEN_SCHEMA = pa.schema([
    pa.field(name, pa.int64() if name in {'floor', 'build_year', 'price_krw', 'previous_deposit_krw', 'monthly_rent_krw', 'deposit_krw', 'previous_monthly_rent_krw'} else
             pa.date32() if name in {"contract_date", "cancellation_date"} else
             pa.timestamp("us", tz="UTC") if name in {"first_seen_at", "last_seen_at"} else
             pa.decimal128(12, 4) if name == "area_m2" else pa.string())
    for name in _FROZEN_FIELDS
])
SCHEMAS = {version: _FROZEN_SCHEMA.with_metadata({b"schema_version": version.encode()})
           for version in ("kapa-runtime-data/1", "kapa-runtime-data/2")}


def schema_for(version):
    if version not in SCHEMAS:
        raise ValueError("unsupported data schema version")
    return SCHEMAS[version]


@dataclass(frozen=True)
class WrittenFile:
    path: Path
    sha256: str
    rows: int


def _safe_path(path: Path) -> None:
    """Refuse links, including Windows junction/reparse point ancestors."""
    import stat
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("linked or reparse storage paths are forbidden")


def _row(record: TransactionRecord) -> dict:
    row = asdict(record)
    area = row["area_m2"]
    if area is not None:
        if not isinstance(area, Decimal) or not area.is_finite():
            raise ValueError("area must be a finite Decimal")
        with localcontext() as ctx:
            ctx.prec = max(32, len(area.as_tuple().digits) + 8)
            if abs(area) >= Decimal("100000000") or area != area.quantize(Decimal("0.0001")):
                raise ValueError("area does not fit decimal128(12,4) exactly")
    for name in _TIME_FIELDS:
        value = row[name]
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must be timezone-aware")
    row["match_status"] = str(row["match_status"])
    row["extra"] = json.dumps(row["extra"], sort_keys=True, ensure_ascii=False,
                              separators=(",", ":"), allow_nan=False)
    return row


def write_partition(records, root, *, asset_type, transaction_type, lawd_code,
                    contract_year, contract_month) -> list[WrittenFile]:
    """Write one explicit partition; reject mismatched records and unsafe components."""
    if not isinstance(asset_type, str) or not re.fullmatch(r"[a-z][a-z0-9_-]*", asset_type):
        raise ValueError("invalid asset_type")
    if transaction_type not in {"sale", "rent"}:
        raise ValueError("invalid transaction_type")
    if not isinstance(lawd_code, str) or not re.fullmatch(r"[0-9]{5}", lawd_code):
        raise ValueError("invalid lawd_code")
    if type(contract_year) is not int or type(contract_month) is not int:
        raise ValueError("partition dates must be integers")
    date(contract_year, contract_month, 1)
    records = list(records)
    for record in records:
        if (record.asset_type, record.transaction_type, record.lawd_code) != (asset_type, transaction_type, lawd_code):
            raise ValueError("record does not match partition")
        if record.contract_date is not None and (record.contract_date.year, record.contract_date.month) != (contract_year, contract_month):
            raise ValueError("record date does not match partition")
    rows = [_row(record) for record in records]
    # Canonical row JSON is a tie breaker; do not drop repeated observations.
    rows.sort(key=lambda row: (row["contract_date"] or date.max, row["record_version_id"],
                              json.dumps(row, default=str, sort_keys=True)))
    table = pa.Table.from_pylist(rows, schema=SCHEMA)
    target = (Path(root).absolute() / "data" / f"asset_type={asset_type}" /
              f"transaction_type={transaction_type}" / f"lawd_code={lawd_code}" /
              f"contract_year={contract_year}" / f"contract_month={contract_month:02d}" /
              "part-00001.parquet")
    _safe_path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    _safe_path(target)
    fd, temporary = tempfile.mkstemp(prefix=".partition-", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            pq.write_table(table, stream, compression="zstd", version="2.6",
                           use_dictionary=False, write_statistics=True, row_group_size=65536)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return [WrittenFile(target, hashlib.sha256(target.read_bytes()).hexdigest(), len(rows))]


def read_partition(path, *, schema_version=SCHEMA_VERSION) -> list[TransactionRecord]:
    """Read a single file without implicit Hive partition inference."""
    path = Path(path).absolute()
    _safe_path(path)
    table = pq.ParquetFile(path).read()
    expected = schema_for(schema_version)
    if table.schema.metadata is None or table.schema.metadata.get(b"schema_version") != schema_version.encode():
        raise ValueError("unsupported parquet schema_version")
    if not table.schema.equals(expected, check_metadata=True):
        raise ValueError("unexpected parquet schema")
    # Windows may have no IANA tzdata installed. UTC is unambiguous; decode
    # physical timestamps without consulting a machine timezone database.
    decoded = table
    for name in _TIME_FIELDS:
        index = decoded.schema.get_field_index(name)
        decoded = decoded.set_column(index, name, decoded.column(name).cast(pa.timestamp("us")))
    records = []
    for row in decoded.to_pylist():
        for name in _TIME_FIELDS:
            row[name] = row[name].replace(tzinfo=timezone.utc)
        row["extra"] = json.loads(row["extra"])
        row["match_status"] = MatchStatus(row["match_status"])
        records.append(TransactionRecord(**row))
    return records
