# SPDX-License-Identifier: GPL-3.0-only
import copy
import hashlib
import json
from pathlib import Path

import pytest

from kapa_runtime.releases.manifest import (
    ManifestError, build_manifest, dumps_manifest, loads_manifest,
    manifest_digest, resolve_snapshot_inventory, validate_manifest,
)
from kapa_runtime.releases import manifest_v2 as v2


def partitions(release="A", region="11110", month="202510", zero=False):
    result = []
    for service in v2.SERVICES:
        kind = service.rsplit("-", 1)[1]
        obs = f"{release}-{region}-{kind}"
        rows = 0 if zero else 1
        result.append(dict(service_id=service, lawd_code=region, deal_ymd=month,
            producer_release_id=release, origin_digest=None, observation_id=obs, run_id=obs,
            fetched_at="2026-11-12T00:00:00+09:00", status="zero" if zero else "success",
            rows=rows, parser_version="parser/1", schema="kapa-runtime-data/2",
            previous_observation=None, evidence_digest="a" * 64,
            files=[dict(path=f"data/observations/{obs}/asset_type=apartment/transaction_type={kind}/lawd_code={region}/contract_year={month[:4]}/contract_month={month[4:]}/part-00001.parquet",
                        sha256="b" * 64, rows=rows)]))
    return result


def keys(parts):
    return [{k: p[k] for k in v2.KEY} for p in parts]


def root():
    m = build_manifest(release_id="A", files=[], parser_version="parser/1",
                       created_at="2026-11-12T00:00:00+09:00")
    parts = partitions()
    m.update(schema_version="kapa-runtime-release/2", release_kind="batch",
             parent_snapshot={"release_id": "parent", "digest": "c" * 64},
             batch_scope={"declared_pairs": [{"lawd_code": "11110", "deal_ymd": "202510"}]},
             selected_partitions=parts, coverage=v2.coverage_for_partitions(parts, keys(parts)),
             coverage_shards=[], files=[dict(path="README.md", sha256="d" * 64, rows=0)])
    return m


def sharded():
    m = root()
    shard = dict(license="SPDX-License-Identifier: GPL-3.0-only", schema_version="kapa-runtime-coverage/1",
                 selected_partitions=m["selected_partitions"], coverage=m["coverage"])
    payload = (json.dumps(shard, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
    sha = hashlib.sha256(payload).hexdigest()
    m.update(selected_partitions=[], coverage=v2.coverage_for_partitions([], []),
             coverage_shards=[dict(path=f"manifests/coverage/{sha}.json", sha256=sha, entries=6)])
    return m, payload


def test_v1_unchanged():
    m = build_manifest(release_id="old", files=[], parser_version="1")
    text = dumps_manifest(m)
    assert dumps_manifest(loads_manifest(text)) == text
    assert resolve_snapshot_inventory(m, lambda _: pytest.fail("v1 loader called")) == m["files"]
    assert manifest_digest(m) == hashlib.sha256(text.encode()).hexdigest()


def test_v2_roundtrip_and_inventory():
    m = root()
    assert loads_manifest(dumps_manifest(m)) == m
    expected = sorted(m["files"] + [f for p in m["selected_partitions"] for f in p["files"]], key=lambda f: f["path"])
    assert resolve_snapshot_inventory(m, None) == expected
    r, payload = sharded()
    calls = []
    def loader(path):
        calls.append(path)
        return payload
    assert resolve_snapshot_inventory(r, loader) == expected
    assert calls == [r["coverage_shards"][0]["path"]]


@pytest.mark.parametrize("mutate", [
    lambda m: m.update(schema_version="kapa-runtime-release/3"),
    lambda m: m.update(schema_version=[]),
    lambda m: m.update(release_kind="other"),
    lambda m: m.update(manifest_sha256="a" * 64),
    lambda m: m["parent_snapshot"].update(release_id="A"),
    lambda m: m["selected_partitions"].append(m["selected_partitions"][0]),
    lambda m: m["selected_partitions"].pop(),
    lambda m: m["batch_scope"]["declared_pairs"].extend([{"lawd_code": "11110", "deal_ymd": "202509"}]),
    lambda m: m["batch_scope"].update(declared_pairs=m["batch_scope"]["declared_pairs"] * 61),
    lambda m: m["coverage"].update(complete=False),
    lambda m: m["selected_partitions"][0].update(rows=True),
    lambda m: m["selected_partitions"][0].update(status="partial"),
    lambda m: m["selected_partitions"][0].update(schema="kapa-runtime-data/1"),
    lambda m: m["selected_partitions"][0].update(origin_digest="a" * 64),
])
def test_invalid_roots(mutate):
    m = root()
    mutate(m)
    with pytest.raises(ManifestError):
        validate_manifest(m)


@pytest.mark.parametrize("change", [
    lambda refs: refs.extend(copy.deepcopy(refs)),
    lambda refs: refs[0].update(path="../bad.json"),
    lambda refs: refs[0].update(path="manifests/coverage/*.json"),
    lambda refs: refs[0].update(entries=4097),
    lambda refs: refs[0].update(entries=True),
    lambda refs: refs.extend(copy.deepcopy(refs) * 1024),
])
def test_invalid_refs(change):
    m, _ = sharded()
    change(m["coverage_shards"])
    with pytest.raises(ManifestError):
        validate_manifest(m)


def test_hash_count_duplicate_and_cycle_rejection():
    m, payload = sharded()
    with pytest.raises(ManifestError, match="digest"):
        resolve_snapshot_inventory(m, lambda _: payload + b" ")
    m["coverage_shards"][0]["entries"] = 5
    with pytest.raises(ManifestError, match="count"):
        resolve_snapshot_inventory(m, lambda _: payload)
    for field, value in [("coverage_shards", m["coverage_shards"]),
                         ("selected_partitions", partitions() * 2)]:
        m, payload = sharded()
        shard = json.loads(payload)
        shard[field] = value
        payload = json.dumps(shard).encode()
        sha = hashlib.sha256(payload).hexdigest()
        m["coverage_shards"][0].update(sha256=sha, path=f"manifests/coverage/{sha}.json")
        with pytest.raises(ManifestError):
            resolve_snapshot_inventory(m, lambda _: payload)
    m, payload = sharded()
    m["selected_partitions"] = partitions()
    with pytest.raises(ManifestError, match="unique"):
        resolve_snapshot_inventory(m, lambda _: payload)


def test_assembly_a_b_c_and_zero():
    a = partitions()
    original = copy.deepcopy(a)
    b = v2.assemble_partitions(a, partitions("B", "26110"))
    assert b[:2] == a
    c = v2.assemble_partitions(b, partitions("C", zero=True))
    assert len(c) == 4 and c[2:] == b[2:]
    assert a == original
    before = v2.coverage_for_partitions(b, keys(b))
    after = v2.coverage_for_partitions(c, keys(c))
    assert before["observed"] == after["observed"]
    assert len(after["zero"]) == 2
    assert after["complete"]
    with pytest.raises(ManifestError, match="Both services"):
        v2.assemble_partitions(b, partitions("C")[:1])


def test_non_cartesian_gaps_and_origins():
    a = partitions()
    other = partitions("B", "26110", "202509")
    declared = keys(a + other)
    c = v2.coverage_for_partitions(a, declared)
    assert c["not_collected"] == keys(other) and not c["complete"]
    m = root()
    for p in other:
        p["origin_digest"] = "e" * 64
    m["selected_partitions"] += other
    m["coverage"] = v2.coverage_for_partitions(m["selected_partitions"], declared)
    validate_manifest(m)
    assert len(m["coverage"]["observed"]) == 4
    assert v2.assemble_partitions(other, a)[2:] == other


def test_metadata_boundary():
    m = root()
    base = len(dumps_manifest(m).encode())
    m["quality"]["padding"] = ""
    overhead = len(dumps_manifest(m).encode()) - base
    m["quality"]["padding"] = "x" * (v2.MAX_METADATA_BYTES - base - overhead)
    assert len(dumps_manifest(m).encode()) == v2.MAX_METADATA_BYTES
    m["quality"]["padding"] += "x"
    with pytest.raises(ManifestError, match="Metadata"):
        validate_manifest(m)
    m, payload = sharded()
    payload += b" " * (v2.MAX_METADATA_BYTES - len(payload))
    sha = hashlib.sha256(payload).hexdigest()
    m["coverage_shards"][0].update(sha256=sha, path=f"manifests/coverage/{sha}.json")
    resolve_snapshot_inventory(m, lambda _: payload)
    with pytest.raises(ManifestError, match="Metadata"):
        resolve_snapshot_inventory(m, lambda _: payload + b" ")


def test_total_entry_limit_and_raw_root_limit(monkeypatch):
    m, _ = sharded()
    monkeypatch.setattr(v2, "MAX_SNAPSHOT_ENTRIES", 5)
    with pytest.raises(ManifestError, match="Snapshot entry"):
        validate_manifest(m)
    monkeypatch.undo()
    text = dumps_manifest(root())
    with pytest.raises(ManifestError, match="Metadata"):
        loads_manifest(text + " " * v2.MAX_METADATA_BYTES)


def test_bootstrap_legacy_paths_and_mixed_zero():
    m = root()
    m["release_kind"] = "bootstrap"
    for p in m["selected_partitions"]:
        p["files"][0]["path"] = p["files"][0]["path"].replace(f"observations/{p['observation_id']}/", "")
        p["producer_release_id"] = "legacy"
        p["origin_digest"] = "e" * 64
    validate_manifest(m)
    m = root()
    p = m["selected_partitions"][0]
    p.update(status="zero", rows=0)
    p["files"][0]["rows"] = 0
    m["coverage"] = v2.coverage_for_partitions(m["selected_partitions"], keys(m["selected_partitions"]))
    validate_manifest(m)
    assert len(m["coverage"]["zero"]) == 1


def test_checked_examples():
    base = Path(__file__).resolve().parents[1] / "examples"
    m = loads_manifest((base / "cumulative-root.example.json").read_text(encoding="utf-8"))
    assert len(resolve_snapshot_inventory(m, lambda p: (base / p).read_bytes())) == 3
