# SPDX-License-Identifier: GPL-3.0-only
"""Administrative commands with a single value-free JSON result envelope."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import ConfigurationError, RuntimeSettings
from .credentials import diagnose, load_secret, redact
from .credentials.loader import ENV_NAMES
from . import pipeline
from .releases.hf import PublishError


def main(argv=None) -> int:
    # Real console/redirection streams must not inherit a Windows legacy code
    # page. StringIO/test capture streams already carry Unicode and may lack it.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="kapa-runtime")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("diagnose", "collect", "build", "publish", "activate", "rollback", "query", "attest-legacy"):
        command = commands.add_parser(name)
        command.add_argument("--config", type=Path)
        if name in ("build", "publish", "activate", "attest-legacy"):
            command.add_argument("--release-id", required=True)
        if name == "collect":
            command.add_argument("--service", required=True, choices=pipeline.SERVICES)
            command.add_argument("--lawd-code", required=True)
            command.add_argument("--deal-ymd", required=True)
            command.add_argument("--release-id", required=True)
        if name == "publish":
            command.add_argument("--dry-run", action="store_true")
        if name == "query":
            command.add_argument("--release-id")
            command.add_argument("--lawd-code", required=True)
            command.add_argument("--limit", type=int, default=100)
            command.add_argument("--cursor")
            for field in ("asset_type", "transaction_type", "contract_date_from", "contract_date_to",
                          "area_m2_min", "area_m2_max", "building_name_contains"):
                command.add_argument("--" + field.replace("_", "-"))
            for field in ("price_krw_min", "price_krw_max"):
                command.add_argument("--" + field.replace("_", "-"), type=int)
            command.add_argument("--include-cancelled", action="store_true")
    args = vars(parser.parse_args(argv))
    name = args.pop("command")
    config = args.pop("config")
    try:
        settings = RuntimeSettings.load(config)
        if name == "diagnose":
            diagnostics = [diagnose(alias, settings) for alias in ENV_NAMES]
            output = {"credentials": diagnostics}
            code = 0 if all(d["status"] == "ok" for d in diagnostics) else 1
        else:
            if name == "query":
                controls = {k: args.pop(k) for k in ("release_id", "limit", "cursor")}
                output = pipeline.query(settings, filters={k: v for k, v in args.items() if v is not None}, **controls)
            else:
                output = getattr(pipeline, name.replace("-", "_"))(settings, **args)
            code = 1 if output.get("state") in ("failed", "partial", "quarantined") else 0
    except ConfigurationError:
        output, code = {"status": "error", "error_code": "configuration_error"}, 2
    except pipeline.PipelineError as exc:
        output, code = {"status": "error", "error_code": exc.error_code}, 1
    except PublishError as exc:
        output, code = {"status": "error", "error_code": "publish_error", "reason": exc.reason}, 1
    except Exception:
        # Never serialize arbitrary exception text, requests, API bodies or paths.
        output, code = {"status": "error", "error_code": "operation_failed"}, 1
    encoded = json.dumps(output, ensure_ascii=False, default=str)
    if name != "diagnose" and "settings" in locals():
        try:
            secrets = []
            for alias in ENV_NAMES:
                if diagnose(alias, settings)["status"] == "ok":
                    secrets.append(load_secret(alias, settings))
            encoded = redact(encoded, secrets)
        except Exception:
            encoded, code = json.dumps({"status": "error", "error_code": "credential_check_failed"}), 1
    print(encoded)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
