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
import pyarrow.dataset as ds

from .snapshot_query import BATCH_SIZE, PROVENANCE_LIMIT, SnapshotIntegrityError, SnapshotInventory

from .parquet import SCHEMA, schema_for, _TIME_FIELDS, _row, _safe_path, read_partition

_SELECT = ", ".join(f"CAST({field.name} AS TIMESTAMP) AS {field.name}" if field.name in _TIME_FIELDS else field.name for field in SCHEMA)

_SORT = "contract_date ASC NULLS LAST, record_version_id ASC, observation_id ASC, source_payload_ref ASC, " + ", ".join(f"{f.name} ASC NULLS LAST" for f in SCHEMA if f.name not in {"contract_date", "record_version_id", "observation_id", "source_payload_ref"})

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

    def __init__(self, snapshot_root, *, data_schema_version="kapa-runtime-data/2", manifest=None):
        schema_for(data_schema_version)
        self.data_schema_version = data_schema_version
        self.root = Path(snapshot_root).absolute()
        _safe_path(self.root)
        if not self.root.is_dir():
            raise ValueError("snapshot root must be a directory")
        self.root = self.root.resolve()
        self.root_manifest_digest = None
        self._snapshot_digest = None
        try:
            self.inventory = SnapshotInventory(self.root, manifest) if manifest is not None else None
        except Exception as exc:
            raise SnapshotIntegrityError('snapshot_inventory_unverified') from exc

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
        payload = _json({"snapshot": snapshot, "query": query, "sort": _SORT, "offset": offset})
        return base64.urlsafe_b64encode(payload).decode()

    def _decode(self, cursor, snapshot, query):
        try:
            if not isinstance(cursor, str) or len(cursor) > 4096:
                raise ValueError
            data = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
            if (not isinstance(data, dict) or set(data) != {"snapshot", "query", "sort", "offset"}
                    or data["sort"] != _SORT or data["snapshot"] != snapshot or data["query"] != query
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
        if self.inventory is None:
            table, snapshot = self._load()
            if self.root_manifest_digest is not None:
                snapshot = hashlib.sha256(_json([self.root_manifest_digest, snapshot])).hexdigest()
        else:
            snapshot = self.inventory.digest
            partitions = self.inventory.select(canonical)
            selected_files = [f for p in partitions for f in p['files']]
            self.inventory.verify(selected_files)
            table = self.inventory.dataset(partitions)
        offset = self._decode(cursor, snapshot, query_digest) if cursor is not None else 0
        where = " AND ".join(clauses) or "TRUE"
        # Strip the already-normalized UTC tag before DuckDB conversion, avoiding
        # an optional ICU/pytz dependency; restore UTC on returned datetimes.
        query_table = table
        for name in _TIME_FIELDS if self.inventory is None else ():
            index = query_table.schema.get_field_index(name)
            query_table = query_table.set_column(index, name, query_table.column(name).cast(pa.timestamp("us")))
        with duckdb.connect(":memory:", config={"enable_external_access": "false", "memory_limit": "256MB", "threads": "1"}) as con:
            def register(count_only=False):
                if self.inventory is None:
                    con.register("transactions", query_table)
                else:
                    # Scanner projection and predicates are pushed into Parquet.
                    expression = None
                    for name, value in canonical.items():
                        if name in _EQUAL:
                            term = ds.field(name) == value
                        elif name in _RANGE:
                            column, op = _RANGE[name]
                            term = ds.field(column) >= value if op == '>=' else ds.field(column) <= value
                        else:
                            continue
                        expression = term if expression is None else expression & term
                    needed = {name for name in canonical if name in _EQUAL}
                    needed.update(_RANGE[name][0] for name in canonical if name in _RANGE)
                    needed.update({'cancellation_date', 'cancellation_status'})
                    if 'building_name_contains' in canonical:
                        needed.add('building_name')
                    columns = {f.name: ds.field(f.name).cast(pa.timestamp('us'))
                               if f.name in _TIME_FIELDS else ds.field(f.name) for f in SCHEMA if not count_only or f.name in needed}
                    if not count_only:
                        # Arrow's fragment filename identifies the actual row owner,
                        # even when observation IDs are reused across partitions.
                        columns = [f.name for f in SCHEMA] + ['__filename']
                    reader = query_table.scanner(columns=columns, filter=expression,
                        batch_size=BATCH_SIZE, batch_readahead=1, fragment_readahead=1,
                        use_threads=False).to_reader()
                    if not count_only:
                        def batches(source):
                            for batch in source:
                                arrays = [batch.column(i).cast(pa.timestamp('us'))
                                          if field.name in _TIME_FIELDS else batch.column(i)
                                          for i, field in enumerate(batch.schema)]
                                yield pa.RecordBatch.from_arrays(arrays, schema=page_schema)
                        page_schema = pa.schema([
                            pa.field(f.name, pa.timestamp('us') if f.name in _TIME_FIELDS else f.type)
                            for f in SCHEMA] + [pa.field('_source_file', pa.string())])
                        reader = pa.RecordBatchReader.from_batches(page_schema, batches(reader))
                    con.register("transactions", reader)
            register(count_only=True)
            excluded_count = con.execute(f"SELECT count(*) FROM transactions WHERE {where} AND {_CANCELLED}", parameters).fetchone()[0] if not include else 0
            register(count_only=True)
            selected = where if include else f"({where}) AND NOT {_CANCELLED}"
            total = con.execute(f"SELECT count(*) FROM transactions WHERE {selected}", parameters).fetchone()[0]
            register()
            projection = _SELECT + (', _source_file' if self.inventory is not None else '')
            result = con.execute(f"SELECT {projection} FROM transactions WHERE {selected} ORDER BY {_SORT} LIMIT ? OFFSET ?", [*parameters, limit, offset])
            names = [column[0] for column in result.description]
            rows = [dict(zip(names, values)) for values in result.fetchall()]
        for row in rows:
            for name in _TIME_FIELDS:
                row[name] = row[name].replace(tzinfo=timezone.utc)
            row["extra"] = json.loads(row["extra"])
        if self.inventory is not None:
            self.inventory.verify(selected_files)
            coverage = {
                'scope': 'digest-bound-exact-sets',
                'details': 'Use get_data_coverage and follow next_cursor.',
            }
            page_files = {Path(r.pop('_source_file')) for r in rows}
            contributors = [p for p in partitions
                            if any(self.root / f['path'] in page_files for f in p['files'])]
        else:
            dates = table.column("contract_date").to_pylist()
            coverage = {
                "lawd_codes": sorted(set(table.column("lawd_code").to_pylist())),
                "months": sorted({value.strftime("%Y-%m") for value in dates if value is not None}),
                "scope": "snapshot-present-records",
            }
        details = {} if self.inventory is None else {
            'snapshot_id': self.inventory.manifest['release_id'],
            'snapshot_digest': snapshot,
            'partition_provenance': self.inventory.provenance(contributors[:PROVENANCE_LIMIT]),
            'partition_provenance_summary': {'total': len(contributors), 'limit': PROVENANCE_LIMIT,
                                             'truncated': len(contributors) > PROVENANCE_LIMIT},
            'limitations': ['Current observations only; archive is not queried.',
                            'Coverage completeness refers only to declared keys.',
                            'Freshness is unknown without an operator SLA.'],
            'legal_verified': False,
            'verification': {'scope': 'selected-files-pre-post', 'files_checked': len(selected_files)}}
        warnings = ["Coverage describes records present, not proof of complete collection."]
        if excluded_count:
            warnings.append("Cancelled transactions excluded by default.")
        return {**details, "rows": rows, "total": total,
                "next_cursor": self._encode(snapshot, query_digest, offset + len(rows)) if offset + len(rows) < total else None,
                "excluded": {"count": excluded_count, "reason": "cancelled" if excluded_count else None},
                "coverage": coverage, "warnings": warnings}
