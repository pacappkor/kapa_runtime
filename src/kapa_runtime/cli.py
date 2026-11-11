# SPDX-License-Identifier: GPL-3.0-only
"""Administrative commands with a single value-free JSON result envelope."""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import json
import logging
import os
import sys
from pathlib import Path

from .config import ConfigurationError, RuntimeSettings
from .credentials import diagnose, load_secret, redact
from .credentials.loader import ENV_NAMES
from . import pipeline
from .releases.hf import PublishError


def _quiet_hub() -> None:
    # Process-local only: never alter the user's global environment or config.
    os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
    os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
    from huggingface_hub import constants
    from huggingface_hub.utils import disable_progress_bars
    from importlib import import_module
    constants.HF_HUB_DISABLE_SYMLINKS_WARNING = True
    constants.HF_HUB_DISABLE_PROGRESS_BARS = True
    # The utility captures this constant at import time; override a preexisting
    # HF_HUB_DISABLE_PROGRESS_BARS=0 for this CLI process as well.
    import_module("huggingface_hub.utils.tqdm").HF_HUB_DISABLE_PROGRESS_BARS = True
    disable_progress_bars()
    # Hub loggers may have inherited a caller's stdout handler. Own this
    # library's CLI handler instead of changing global logging configuration.
    logger = logging.getLogger("huggingface_hub")
    logger.handlers = [logging.StreamHandler(sys.stderr)]
    logger.propagate = False
    for name, child in logging.root.manager.loggerDict.items():
        if name.startswith("huggingface_hub.") and isinstance(child, logging.Logger):
            child.handlers = []
            child.propagate = True


def main(argv=None) -> int:
    _quiet_hub()
    # Real console/redirection streams must not inherit a Windows legacy code
    # page. StringIO/test capture streams already carry Unicode and may lack it.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="kapa-runtime")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("diagnose", "collect", "build", "publish", "activate", "rollback", "query", "attest-legacy", "plan", "collect-batch"):
        command = commands.add_parser(name)
        command.add_argument("--config", type=Path)
        if name in ("build", "publish", "activate", "attest-legacy"):
            command.add_argument("--release-id", required=True)
        if name == "collect":
            command.add_argument("--service", required=True, choices=pipeline.SERVICES)
            command.add_argument("--lawd-code", required=True)
            command.add_argument("--deal-ymd", required=True)
            command.add_argument("--release-id", required=True)
        if name == "collect-batch":
            command.add_argument("--job-id", required=True)
            command.add_argument("--dry-run", action="store_true")
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
            diagnostics = [diagnose(alias, settings)
                           for alias in pipeline._secret_aliases(settings, hf=True)]
            output = {"credentials": diagnostics}
            code = 0 if all(d["status"] == "ok" for d in diagnostics) else 1
        else:
            if name in ("plan", "collect-batch"):
                from . import accumulation
                output = getattr(accumulation, name.replace("-", "_"))(settings, **args)
            elif name == "query":
                controls = {k: args.pop(k) for k in ("release_id", "limit", "cursor")}
                with redirect_stdout(sys.stderr):
                    output = pipeline.query(settings, filters={k: v for k, v in args.items() if v is not None}, **controls)
            else:
                with redirect_stdout(sys.stderr):
                    output = getattr(pipeline, name.replace("-", "_"))(settings, **args)
            code = 1 if output.get("state") in ("failed", "partial", "quarantined", "blocked") else 0
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
    if name not in ("diagnose", "plan", "collect-batch") and "settings" in locals():
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
