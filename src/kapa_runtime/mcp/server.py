# SPDX-License-Identifier: GPL-3.0-only
"""Stdio tools over verified local data. No collector, credentials or HF imports."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import InvalidOperation
import json
import sys
from uuid import uuid4

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ..config import ConfigurationError, RuntimeSettings
from ..registry import ServiceRegistry
from ..releases.manifest import manifest_digest
from ..storage.active import ActiveSnapshotError, search_active, coverage_active


def envelope(status, data=None, *, manifest=None, result=None):
    result = result or {}
    if data is result:
        # Query payload and envelope metadata have one canonical location each.
        data = {name: result[name] for name in ('rows', 'total') if name in result}
    refs = manifest["hugging_face_revisions"] if manifest else []
    value = {
        "schema_version": "kapa-runtime-mcp/1", "request_id": str(uuid4()),
        "status": status, "data": data,
        "provenance": {"release_id": manifest["release_id"] if manifest else None,
                       "manifest_sha256": manifest_digest(manifest) if manifest else None,
                       "hugging_face_revisions": refs},
        "snapshot_refs": refs,
        "partition_provenance": result.get("partition_provenance", []),
        "partition_provenance_summary": result.get("partition_provenance_summary", {}),
        "excluded": result.get("excluded", {"count": 0, "reason": None}),
        "verification": result.get("verification", {}),
        "limitations": result.get("limitations", ["Coverage is not a legal verification."]),
        "coverage": {"release": ({"scope": "digest-bound-exact-sets",
                                   "details": "Use get_data_coverage and follow next_cursor."}
                                  if manifest.get("schema_version") == "kapa-runtime-release/2"
                                  else manifest["coverage"]),
                     "query": result.get("coverage", {})} if manifest else {},
        "warnings": result.get("warnings", []),
        "unresolved_gaps": manifest["unresolved_gaps"] if manifest else [],
        "next_cursor": result.get("next_cursor"),
        "observed_at": datetime.now(timezone.utc).isoformat(), "legal_verified": False,
    }
    # Preserve exact decimals as strings, dates as ISO strings, and Korean text.
    return json.loads(json.dumps(value, ensure_ascii=False, default=str, allow_nan=False))


def create_server(settings: RuntimeSettings) -> MCPServer:
    server = MCPServer("kapa-runtime-mcp", instructions=(
        "Read only the local verified ACTIVE snapshot. No collection, network, credentials, "
        "publishing or legal verification. Preserve coverage, exclusions and unresolved gaps."),
        log_level="WARNING")
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                           idempotentHint=True, openWorldHint=False)

    @server.tool(annotations=read)
    def list_data_capabilities(service_id: str | None = None) -> dict:
        """List registry capabilities; quotas are user-reported, not live remaining budgets."""
        registry = ServiceRegistry.load()
        services = [spec for spec in registry.services.values()
                    if service_id is None or spec.service_id == service_id]
        data = {"services": [{
            "service_id": s.service_id, "dataset_id": s.dataset_id, "provider": s.provider,
            "approval_status": s.approval_status, "operation_verified": s.operation_verified,
            "last_connection_check": s.last_connection_check,
            "quota": s.quota, "quota_verification": "user-reported-not-provider-verified",
            "required_params": list(s.required_params), "param_patterns": s.param_patterns,
            "page_size_max": s.page_size_max,
            "storage_and_redistribution_reviewed": s.storage_and_redistribution_reviewed,
        } for s in services]}
        return envelope("ok" if services else "no_data", data, result={"warnings": [
            "Registry capabilities do not establish snapshot coverage or current provider availability.",
            "Quotas are user-reported; remaining budgets are not queried."]})

    @server.tool(annotations=read)
    def search_transactions(filters: dict | None = None, limit: int = 100,
                            cursor: str | None = None) -> dict:
        """Search ACTIVE only. Allowlisted filters: asset_type, transaction_type, lawd_code,
        contract_date_from/to (ISO), price_krw_min/max (integer KRW), area_m2_min/max
        (exact decimal strings/integers), building_name_contains, include_cancelled (boolean).
        Limit 1..1000; cursor is bound to the snapshot and filters. No SQL or paths.
        """
        try:
            manifest, result = search_active(settings, {} if filters is None else filters,
                                             limit=limit, cursor=cursor)
            return envelope("ok" if result["total"] else "no_data", result,
                            manifest=manifest, result=result)
        except ActiveSnapshotError as exc:
            code = str(exc)
            return envelope("no_active_release" if code == "no_active_release" else "error",
                            {"error_code": code})
        except (ValueError, TypeError, InvalidOperation, OverflowError):
            return envelope("error", {"error_code": "invalid_query"})
        except Exception:
            return envelope("error", {"error_code": "query_failed"})

    @server.tool(annotations=read)
    def get_data_coverage(filters: dict | None = None, limit: int = 100,
                          cursor: str | None = None) -> dict:
        """Read exact ACTIVE coverage keys, zero/gaps and observation provenance.

        Filters: service_id, lawd_code, contract_date_from/to. Pages contain at most
        limit distinct keys (1..1000), not a Cartesian product. Digest-bound cursor.
        Staleness is unknown without a freshness SLA; fetched_at is preserved.
        """
        try:
            manifest, result = coverage_active(settings, filters, limit=limit, cursor=cursor)
            status = ('no_data' if manifest['schema_version'] == 'kapa-runtime-release/2'
                      and not result['total'] else 'ok')
            return envelope(status, result, manifest=manifest, result=result)
        except ActiveSnapshotError as exc:
            code = str(exc)
            return envelope("no_active_release" if code == "no_active_release" else "error",
                            {"error_code": code})
        except (ValueError, TypeError, InvalidOperation, OverflowError):
            return envelope("error", {"error_code": "invalid_query"})
        except Exception:
            return envelope("error", {"error_code": "query_failed"})

    return server


def main():
    try:
        settings = RuntimeSettings.load()
    except ConfigurationError:
        print("KAPA_RUNTIME_CONFIG must identify a valid explicit runtime configuration", file=sys.stderr)
        return 2
    create_server(settings).run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
