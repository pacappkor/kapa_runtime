# SPDX-License-Identifier: GPL-3.0-only
"""Rebuild the offline reference from a locally supplied MOIS full-data ZIP or TSV.

No network access. The ZIP must contain exactly one member; no extraction occurs.
"""
from __future__ import annotations

import argparse
import csv
from datetime import date
import hashlib
import io
import json
from pathlib import Path
import re
import zipfile

SOURCE_URL = "https://www.code.go.kr/etc/codeFullDown.do?codeseId=%EB%B2%95%EC%A0%95%EB%8F%99%EC%BD%94%EB%93%9C"
RULE = (
    "Unique first five digits of active (존재) ten-digit legal district codes; "
    "exclude sido aggregates ending 000. Retain all other active prefixes, including "
    "city aggregates and Sejong 36110; no unverified RTMS-specific exclusions. "
    "Use the active prefix+00000 row name (otherwise shortest active name, lexical tie break). "
    "Historical abolished/split-code handling is unresolved."
)


def validate_download_date(value: str) -> str:
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("download date must be YYYY-MM-DD")
    date.fromisoformat(value)
    return value


def generate(raw: bytes, download_date: str) -> tuple[dict, dict]:
    validate_download_date(download_date)
    payload = raw
    member = None
    if zipfile.is_zipfile(io.BytesIO(raw)):
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            if len(archive.infolist()) != 1 or archive.infolist()[0].file_size > 20_000_000:
                raise ValueError("expected one bounded full-data member")
            member = archive.infolist()[0].filename
            payload = archive.read(member)
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = payload.decode("cp949")
    reader = csv.DictReader(io.StringIO(text), delimiter="\t")
    if reader.fieldnames != ["법정동코드", "법정동명", "폐지여부"]:
        raise ValueError("unexpected MOIS full-data columns")
    grouped: dict[str, list[tuple[str, str]]] = {}
    for row in reader:
        code, name, state = (row[key].strip() for key in reader.fieldnames)
        if not re.fullmatch(r"[0-9]{10}", code) or not name or state not in {"존재", "폐지"}:
            raise ValueError("invalid MOIS full-data row")
        if state == "존재" and not code[:5].endswith("000"):
            grouped.setdefault(code[:5], []).append((code, name))
    if not grouped:
        raise ValueError("empty MOIS full-data reference")
    codes = []
    for prefix, rows in sorted(grouped.items()):
        name = min(rows, key=lambda item: (item[0] != prefix + "00000", len(item[1]), item[1]))[1]
        codes.append({"code": prefix, "name": name, "sido": name.split()[0], "active": True})
    table = {"license": "SPDX-License-Identifier: GPL-3.0-only", "schema": "kapa-runtime-lawd/1", "codes": codes}
    provenance = {
        "license": "SPDX-License-Identifier: GPL-3.0-only",
        "source_name": "행정안전부 행정표준코드관리시스템 법정동코드 전체자료",
        "source_url": SOURCE_URL,
        "download_date": download_date,
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "raw_bytes": len(raw),
        "archive_member": member,
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
        "payload_bytes": len(payload),
        "generation_rule": RULE,
        "historical_codes": "unresolved",
        "count": len(codes),
    }
    return table, provenance


def serialize(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("raw", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--download-date", required=True, type=validate_download_date)
    args = parser.parse_args()
    table, provenance = generate(args.raw.read_bytes(), args.download_date)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for name, value in (("lawd_codes.json", table), ("lawd_codes.provenance.json", provenance)):
        (args.output_dir / name).write_text(serialize(value), encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
