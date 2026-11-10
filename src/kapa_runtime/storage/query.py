# SPDX-License-Identifier: GPL-3.0-only
"""Bound, allowlisted DuckDB queries over a fixed local snapshot."""
from __future__ import annotations

import base64
from datetime import date, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path

import duckdb
import pyarrow as pa

from .parquet import SCHEMA, schema_for, _TIME_FIELDS, _row, _safe_path, read_partition

_SELECT = ", ".join(f"CAST({field.name} AS TIMESTAMP) AS {field.name}" if field.name in _TIME_FIELDS else field.name for field in SCHEMA)

_EQUAL = {"asset_type", "transaction_type", "lawd_code"}
_RANGE = {
    "contract_date_from": ("contract_date", ">="),
    "contract_date_to": ("contract_date", "<="),
    "price_krw_min": ("price_krw", ">="),
    "price_krw_max": ("price_krw", "<="),
    "area_m2_min": ("area_m2", ">="),
    "area_m2_max": ("area_m2", "<="),
}
_ALLOWED = _EQUAL | _RANGE.keys() | {"building_name_contains", "include_cancelled"}
_CANCELLED = "(cancellation_date IS NOT NULL OR (cancellation_status IS NOT NULL AND trim(cancellation_status) NOT IN ('', 'N', 'n', '0')))"


def _json(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str,
                      ensure_ascii=False, allow_nan=False).encode()


class TransactionQuery:
    """Rehash each search; portable unsigned cursors bind snapshot and query.

    A cursor grants no authority: changing its offset only selects another page
    of the same already-readable query. No secret or per-instance key is needed.
    """

    def __init__(self, snapshot_root, *, data_schema_version="kapa-runtime-data/2"):
        schema_for(data_schema_version)
        self.data_schema_version = data_schema_version
        self.root = Path(snapshot_root).absolute()
        _safe_path(self.root)
        if not self.root.is_dir():
            raise ValueError("snapshot root must be a directory")
        self.root = self.root.resolve()
        self._snapshot_digest = None

    def _load(self):
        _safe_path(self.root)
        files = []
        # Inspect every entry, not only matching files, so linked directories cannot hide data.
        for path in self.root.rglob("*"):
            _safe_path(path)
            if not path.resolve().is_relative_to(self.root):
                raise ValueError("snapshot path escapes root")
            if path.is_file() and path.suffix == ".parquet":
                files.append(path)
        digest = hashlib.sha256()
        rows = []
        for path in sorted(files):
            digest.update(_json([path.relative_to(self.root).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest()]))
            rows.extend(_row(record) for record in read_partition(path, schema_version=self.data_schema_version))
        current = digest.hexdigest()
        if self._snapshot_digest is not None and self._snapshot_digest != current:
            raise ValueError("snapshot changed; create a new reader")
        self._snapshot_digest = current
        return pa.Table.from_pylist(rows, schema=SCHEMA), current

    def _encode(self, snapshot, query, offset):
        payload = _json({"snapshot": snapshot, "query": query, "offset": offset})
        return base64.urlsafe_b64encode(payload).decode()

    def _decode(self, cursor, snapshot, query):
        try:
            if not isinstance(cursor, str) or len(cursor) > 4096:
                raise ValueError
            data = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
            if (not isinstance(data, dict) or set(data) != {"snapshot", "query", "offset"}
                    or data["snapshot"] != snapshot or data["query"] != query
                    or type(data["offset"]) is not int or not 0 <= data["offset"] <= 2**63 - 1):
                raise ValueError
            return data["offset"]
        except (ValueError, KeyError, TypeError, UnicodeError) as exc:
            raise ValueError("invalid cursor for this snapshot/query") from exc

    def search(self, filters: dict, *, limit=100, cursor=None) -> dict:
        if not isinstance(filters, dict) or filters.keys() - _ALLOWED:
            raise ValueError("unsupported filters")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        include = filters.get("include_cancelled", False)
        if type(include) is not bool:
            raise ValueError("include_cancelled must be boolean")
        clauses, parameters, canonical = [], [], {"include_cancelled": include}
        for key, value in sorted(filters.items()):
            if key == "include_cancelled":
                continue
            if key in _EQUAL or key == "building_name_contains":
                if not isinstance(value, str):
                    raise ValueError("text filters require strings")
                clause = f"{key} = ?" if key in _EQUAL else "contains(building_name, ?)"
            else:
                column, operator = _RANGE[key]
                if column == "contract_date":
                    if type(value) is str:
                        value = date.fromisoformat(value)
                    if type(value) is not date:
                        raise ValueError("date filters require ISO dates")
                elif column == "price_krw":
                    if type(value) is not int:
                        raise ValueError("price filters require integer KRW")
                else:
                    if not isinstance(value, (str, Decimal, int)) or isinstance(value, bool):
                        raise ValueError("area filters require exact decimals")
                    value = Decimal(value)
                    if not value.is_finite():
                        raise ValueError("area filters must be finite")
                clause = f"{column} {operator} ?"
            clauses.append(clause)
            parameters.append(value)
            canonical[key] = value
        query_digest = hashlib.sha256(_json(canonical)).hexdigest()
        table, snapshot = self._load()
        offset = self._decode(cursor, snapshot, query_digest) if cursor is not None else 0
        where = " AND ".join(clauses) or "TRUE"
        # Strip the already-normalized UTC tag before DuckDB conversion, avoiding
        # an optional ICU/pytz dependency; restore UTC on returned datetimes.
        query_table = table
        for name in _TIME_FIELDS:
            index = query_table.schema.get_field_index(name)
            query_table = query_table.set_column(index, name, query_table.column(name).cast(pa.timestamp("us")))
        with duckdb.connect(":memory:", config={"enable_external_access": "false"}) as con:
            con.register("transactions", query_table)
            excluded_count = con.execute(f"SELECT count(*) FROM transactions WHERE {where} AND {_CANCELLED}", parameters).fetchone()[0] if not include else 0
            selected = where if include else f"({where}) AND NOT {_CANCELLED}"
            total = con.execute(f"SELECT count(*) FROM transactions WHERE {selected}", parameters).fetchone()[0]
            result = con.execute(f"SELECT {_SELECT} FROM transactions WHERE {selected} ORDER BY contract_date ASC NULLS LAST, record_version_id ASC, observation_id ASC, source_payload_ref ASC LIMIT ? OFFSET ?", [*parameters, limit, offset])
            names = [column[0] for column in result.description]
            rows = [dict(zip(names, values)) for values in result.fetchall()]
        for row in rows:
            for name in _TIME_FIELDS:
                row[name] = row[name].replace(tzinfo=timezone.utc)
            row["extra"] = json.loads(row["extra"])
        dates = table.column("contract_date").to_pylist()
        coverage = {
            "lawd_codes": sorted(set(table.column("lawd_code").to_pylist())),
            "months": sorted({value.strftime("%Y-%m") for value in dates if value is not None}),
            "scope": "snapshot-present-records",
        }
        warnings = ["Coverage describes records present, not proof of complete collection."]
        if excluded_count:
            warnings.append("Cancelled transactions excluded by default.")
        return {"rows": rows, "total": total,
                "next_cursor": self._encode(snapshot, query_digest, offset + len(rows)) if offset + len(rows) < total else None,
                "excluded": {"count": excluded_count, "reason": "cancelled" if excluded_count else None},
                "coverage": coverage, "warnings": warnings}
