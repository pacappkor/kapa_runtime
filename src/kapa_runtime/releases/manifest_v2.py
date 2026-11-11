# SPDX-License-Identifier: GPL-3.0-only
"""Contract-only cumulative snapshots; deliberately not wired into the pipeline."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime

from .manifest import ManifestError, _json, _pairs, _constant, relative_path

MAX_METADATA_BYTES = 16 * 1024 * 1024
MAX_SHARDS = 1024
MAX_SHARD_ENTRIES = 4096
MAX_SNAPSHOT_ENTRIES = 250_000
MAX_BATCH_PAIRS = 60
SERVICES = ("molit-apartment-rent", "molit-apartment-sale")
KEY = {"service_id", "lawd_code", "deal_ymd"}
COVERAGE = {"declared", "observed", "zero", "not_collected"}


def require(condition, message):
    if not condition:
        raise ManifestError(message)


def identifier(value):
    require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value), "Invalid identifier")


def digest(value):
    require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value), "Invalid digest")


def pair(value):
    require(isinstance(value, dict), "Invalid pair")
    code, month = value.get("lawd_code"), value.get("deal_ymd")
    require(isinstance(code, str) and re.fullmatch(r"[0-9]{5}", code), "Invalid region")
    require(isinstance(month, str) and re.fullmatch(r"[0-9]{6}", month), "Invalid month")
    require(1 <= int(month[:4]) and 1 <= int(month[4:]) <= 12, "Invalid month")
    return code, month


def key(value):
    p = pair(value)
    require(value.get("service_id") in SERVICES, "Invalid service")
    return (*p, value["service_id"])


def ordered(values, check, limit=MAX_SNAPSHOT_ENTRIES):
    require(isinstance(values, list) and len(values) <= limit, "Entry limit exceeded")
    keys = [check(v) for v in values]
    require(keys == sorted(set(keys)), "Entries must be unique and sorted")
    return set(keys)


def complete_pairs(keys):
    require(all((c, m, s) in keys for c, m, _ in keys for s in SERVICES), "Both services required")


def files(values):
    def check(f):
        require(isinstance(f, dict) and set(f) == {"path", "sha256", "rows"}, "Invalid file")
        digest(f["sha256"])
        require(type(f["rows"]) is int and f["rows"] >= 0, "Invalid rows")
        return relative_path(f["path"])
    return ordered(values, check)


def partition(p):
    fields = KEY | {"producer_release_id", "origin_digest", "observation_id", "run_id", "fetched_at",
                    "status", "rows", "parser_version", "schema", "files", "previous_observation", "evidence_digest"}
    require(isinstance(p, dict) and set(p) == fields, "Invalid partition fields")
    k = key(p)
    for name in ("producer_release_id", "observation_id", "run_id"):
        identifier(p[name])
    require(isinstance(p["parser_version"], str) and bool(p["parser_version"].strip()), "Invalid parser")
    if p["origin_digest"] is not None:
        digest(p["origin_digest"])
    digest(p["evidence_digest"])
    require(p["schema"] == "kapa-runtime-data/2", "Mixed or unknown schema")
    try:
        require(datetime.fromisoformat(p["fetched_at"]).utcoffset() is not None, "Naive fetched_at")
    except (ValueError, TypeError):
        raise ManifestError("Invalid fetched_at") from None
    require(type(p["rows"]) is int and p["rows"] >= 0, "Invalid rows")
    require(p["status"] in ("success", "zero") and (p["status"] == "zero") == (p["rows"] == 0), "Invalid observation status")
    files(p["files"])
    require(bool(p["files"]) and sum(f["rows"] for f in p["files"]) == p["rows"], "Partition rows mismatch")
    kind = "rent" if k[2] == SERVICES[0] else "sale"
    suffix = (f"asset_type=apartment/transaction_type={kind}/lawd_code={k[0]}/"
              f"contract_year={k[1][:4]}/contract_month={k[1][4:]}/part-[0-9]{{5}}\\.parquet")
    for f in p["files"]:
        require(any(re.fullmatch(base + suffix, f["path"]) for base in
                    (f"data/observations/{p['observation_id']}/", "data/")), "Invalid observation path")
    prev = p["previous_observation"]
    if prev is not None:
        require(isinstance(prev, dict) and set(prev) == {"observation_id", "producer_release_id", "digest"}, "Invalid previous observation")
        identifier(prev["observation_id"])
        identifier(prev["producer_release_id"])
        digest(prev["digest"])
        require(prev["observation_id"] != p["observation_id"], "Self observation reference")
    return k


def coverage(c):
    require(isinstance(c, dict) and set(c) == COVERAGE | {"complete"}, "Invalid coverage fields")
    sets = {}
    for name in sorted(COVERAGE):
        def check(v):
            require(isinstance(v, dict) and set(v) == KEY, "Invalid coverage key")
            return key(v)
        sets[name] = ordered(c[name], check)
        if name != "zero":
            complete_pairs(sets[name])
    require(type(c["complete"]) is bool, "Invalid completeness")
    require(sets["zero"] <= sets["observed"] and
            sets["not_collected"] == sets["declared"] - sets["observed"] and
            c["complete"] == (not sets["not_collected"]), "Inconsistent coverage")
    return sets


def validate_block(partitions, cov):
    """Validate one pair-atomic root/shard block, without global batch rules."""
    keys = ordered(partitions, partition)
    complete_pairs(keys)
    sets = coverage(cov)
    require(keys == sets["observed"], "Selected/observed mismatch")
    require(sets["zero"] == {key(p) for p in partitions if p["status"] == "zero"}, "Zero mismatch")
    return keys, sets


def validate_selection(m, partitions, cov):
    keys, sets = validate_block(partitions, cov)
    batch = {(*pair(p), s) for p in m["batch_scope"]["declared_pairs"] for s in SERVICES}
    require(batch <= keys and batch <= sets["declared"], "Incomplete batch")
    for p in partitions:
        current = p["producer_release_id"] == m["release_id"]
        if m["release_kind"] == "batch":
            require(current == (key(p) in batch), "Incorrect batch producer")
        require((p["origin_digest"] is None) == current, "Invalid producer origin binding")
        if current and m["release_kind"] == "batch":
            require(all(f["path"].startswith("data/observations/") for f in p["files"]), "New observations must be immutable")
    files(sorted(m["files"] + [f for p in partitions for f in p["files"]], key=lambda f: f["path"]))


def validate_root(m):
    require(len((_json(m) + "\n").encode("utf-8")) <= MAX_METADATA_BYTES, "Metadata limit exceeded")
    fields = {"schema_version", "release_id", "state", "created_at", "source_runs", "github_revisions",
              "hugging_face_revisions", "parser_version", "data_schema_version", "files", "coverage",
              "quality", "unresolved_gaps", "legal_verified", "release_kind", "parent_snapshot",
              "batch_scope", "selected_partitions", "coverage_shards"}
    require(set(m) == fields, "Invalid v2 root fields (self hash/commit forbidden)")
    identifier(m["release_id"])
    require(m["release_kind"] in ("batch", "bootstrap"), "Invalid release kind")
    parent = m["parent_snapshot"]
    require(isinstance(parent, dict) and set(parent) == {"release_id", "digest"}, "Invalid parent")
    identifier(parent["release_id"])
    digest(parent["digest"])
    require(parent["release_id"] != m["release_id"], "Self parent")
    require(m["data_schema_version"] == "kapa-runtime-data/2", "Invalid snapshot schema")
    scope = m["batch_scope"]
    require(isinstance(scope, dict) and set(scope) == {"declared_pairs"}, "Invalid scope")
    def declared(p):
        require(isinstance(p, dict) and set(p) == {"lawd_code", "deal_ymd"}, "Invalid declared pair")
        return pair(p)
    ordered(scope["declared_pairs"], declared, MAX_BATCH_PAIRS)
    require(bool(scope["declared_pairs"]), "Empty batch")
    validate_block(m["selected_partitions"], m["coverage"])
    def ref(r):
        require(isinstance(r, dict) and set(r) == {"path", "sha256", "entries"}, "Invalid shard reference")
        digest(r["sha256"])
        require(relative_path(r["path"]) == f"manifests/coverage/{r['sha256']}.json", "Invalid shard path")
        require(type(r["entries"]) is int and 0 < r["entries"] <= MAX_SHARD_ENTRIES, "Shard entry limit exceeded")
        return r["path"]
    ordered(m["coverage_shards"], ref, MAX_SHARDS)
    count = len(m["selected_partitions"]) + sum(len(m["coverage"][n]) for n in COVERAGE)
    require(count + sum(r["entries"] for r in m["coverage_shards"]) <= MAX_SNAPSHOT_ENTRIES, "Snapshot entry limit exceeded")
    files(m["files"])
    require(all(not f["path"].startswith("data/") for f in m["files"]), "Root files are metadata only")
    if not m["coverage_shards"]:
        validate_selection(m, m["selected_partitions"], m["coverage"])


def resolve_snapshot(manifest, loader):
    """Load flat explicit shards only. Caller supplies safe, revision-pinned bytes loader.

    A hash is not an origin trust proof. No parent lookup or glob is performed.
    validate_manifest validates the root only; this validates the full snapshot.
    """
    from .manifest import validate_manifest
    m = validate_manifest(manifest)
    if m["schema_version"] == "kapa-runtime-release/1":
        return m
    partitions = list(m["selected_partitions"])
    cov = {n: list(m["coverage"][n]) for n in COVERAGE}
    owned_pairs = {pair(v) for n in ("declared", "observed") for v in cov[n]}
    for ref in m["coverage_shards"]:
        payload = loader(ref["path"])
        require(isinstance(payload, bytes) and len(payload) <= MAX_METADATA_BYTES, "Metadata limit exceeded")
        require(hashlib.sha256(payload).hexdigest() == ref["sha256"], "Shard digest mismatch")
        try:
            shard = json.loads(payload.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
        except (UnicodeError, ValueError, TypeError):
            raise ManifestError("Invalid shard JSON") from None
        require(isinstance(shard, dict) and set(shard) == {"schema_version", "selected_partitions", "coverage", "license"}
                and shard["schema_version"] == "kapa-runtime-coverage/1"
                and shard["license"] == "SPDX-License-Identifier: GPL-3.0-only", "Invalid flat shard")
        ordered(shard["selected_partitions"], partition, MAX_SHARD_ENTRIES)
        _, sets = validate_block(shard["selected_partitions"], shard["coverage"])
        shard_pairs = {k[:2] for k in sets["declared"] | sets["observed"]}
        require(owned_pairs.isdisjoint(shard_pairs), "Pair must belong to a unique block")
        owned_pairs.update(shard_pairs)
        count = len(shard["selected_partitions"]) + sum(len(shard["coverage"][n]) for n in COVERAGE)
        require(count == ref["entries"], "Shard entry count mismatch")
        partitions.extend(shard["selected_partitions"])
        for n in COVERAGE:
            cov[n].extend(shard["coverage"][n])
    partitions.sort(key=key)
    for values in cov.values():
        values.sort(key=key)
    cov["complete"] = not cov["not_collected"]
    validate_selection(m, partitions, cov)
    return {**m, "selected_partitions": partitions, "coverage": cov}


def resolve_snapshot_inventory(manifest, loader):
    m = resolve_snapshot(manifest, loader)
    if m["schema_version"] == "kapa-runtime-release/1":
        return m["files"]
    return sorted(m["files"] + [f for p in m["selected_partitions"] for f in p["files"]], key=lambda f: f["path"])


def assemble_partitions(parent, batch):
    """Pure right-biased K union. Caller binds inherited origins before assembly."""
    parent, batch = json.loads(_json(parent)), json.loads(_json(batch))
    for values in (parent, batch):
        complete_pairs(ordered(values, partition))
    selected = {key(p): p for p in parent}
    selected.update({key(p): p for p in batch})
    result = [selected[k] for k in sorted(selected)]
    files(sorted([f for p in result for f in p["files"]], key=lambda f: f["path"]))
    return result


def coverage_for_partitions(partitions, declared):
    """Exact K sets: normal zero is observed, re-observation adds no coverage."""
    complete_pairs(ordered(partitions, partition))
    ordered(declared, key)
    observed = [{k: p[k] for k in KEY} for p in partitions]
    keys = {key(p) for p in partitions}
    gaps = [dict(p) for p in declared if key(p) not in keys]
    result = {"declared": json.loads(_json(declared)), "observed": observed,
              "zero": [{k: p[k] for k in KEY} for p in partitions if p["status"] == "zero"],
              "not_collected": gaps, "complete": not gaps}
    coverage(result)
    return result
