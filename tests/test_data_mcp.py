# SPDX-License-Identifier: GPL-3.0-only
"""Official SDK protocol coverage; synthetic local snapshots, no provider calls."""
from contextlib import closing
import asyncio
import hashlib
import json
import sqlite3
import subprocess
import sys

import pytest

mcp = pytest.importorskip("mcp")
from mcp import Client, StdioServerParameters

from kapa_runtime.config import RuntimeSettings
from kapa_runtime.mcp.server import create_server
from kapa_runtime.releases.manifest import (
    ManifestError, build_manifest, dump_manifest, manifest_digest, validate_manifest,
)

from test_parquet import record, write


@pytest.fixture
def active(tmp_path):
    settings = RuntimeSettings(tmp_path, tmp_path / "data")
    root = settings.snapshots_dir / "synthetic"
    write([record("1", building_name="한글 아파트"), record("2"),
           record("3", cancellation_status="O")], root)
    files = [{"path": p.relative_to(root).as_posix(), "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
              "rows": 3} for p in root.rglob("*.parquet")]
    manifest = build_manifest(release_id="synthetic", files=files, parser_version="synthetic/1",
        state="published", github_revisions={"pacappkor/kapa_runtime": "a" * 40,
            "pacappkor/kapa_skills": "c" * 40, "pacappkor/kapa_knowledge": "d" * 40,
            "pacappkor/kapa_edu_1": None, "pacappkor/kapa_edu_2": None,
            "pacappkor/kapa_mcp": None},
        hugging_face_revisions=[{"repo_id": "fixture/data", "commit_sha": "b" * 40}],
        coverage={"services": ["molit-apartment-sale"], "months": ["2026-10"]},
        unresolved_gaps=["Historical availability unknown"], quality={"passed": True})
    settings.manifests_dir.mkdir(parents=True)
    dump_manifest(manifest, settings.active_manifest)
    settings.state_db.parent.mkdir(parents=True)
    with closing(sqlite3.connect(settings.state_db)) as con:
        con.execute("CREATE TABLE releases (release_id TEXT, guard_evidence TEXT)")
        con.execute("INSERT INTO releases VALUES (?, ?)", ("synthetic", json.dumps([
            {"state": "published", "evidence": {"commit_reread": True,
             "published_manifest_sha256": manifest_digest(manifest)}}])))
        con.commit()
    return settings


@pytest.mark.parametrize("repo,sha", [
    ("runtime", "a" * 40),
    ("pacappkor/kapa_runtime", None),
    ("fixture/unknown", None),
    ("pacappkor/kapa_skills", "main"),
])
def test_active_manifest_rejects_invalid_source_revisions(active, repo, sha):
    manifest = json.loads(active.active_manifest.read_text(encoding="utf-8"))
    manifest["github_revisions"][repo] = sha
    with pytest.raises(ManifestError):
        validate_manifest(manifest)


def test_read_does_not_verify_operator_workspace(active, monkeypatch):
    from kapa_runtime.releases import workspace
    from kapa_runtime.storage.active import search_active

    def forbidden(*args, **kwargs):
        pytest.fail("MCP reads must not inspect operator checkouts")

    for name in ("git", "revision", "capture", "compatibility", "verify"):
        monkeypatch.setattr(workspace, name, forbidden)
    # No checkouts exist, despite required siblings having recorded revisions.
    assert not (active.workspace / "kapa_knowledge").exists()
    before = {p.relative_to(active.data_root): p.read_bytes()
              for p in active.data_root.rglob("*") if p.is_file()}
    manifest, result = search_active(active, {})
    assert manifest["github_revisions"]["pacappkor/kapa_edu_1"] is None
    assert result["total"] == 2
    after = {p.relative_to(active.data_root): p.read_bytes()
             for p in active.data_root.rglob("*") if p.is_file()}
    assert before == after


def unpack(result):
    assert not result.is_error
    return json.loads(result.content[0].text)


def test_official_in_memory_protocol(active):
    async def exercise():
        async with Client(create_server(active), mode="legacy") as client:
            tools = await client.list_tools()
            assert {tool.name for tool in tools.tools} == {"list_data_capabilities", "search_transactions", "get_data_coverage"}
            assert all(t.annotations.read_only_hint and not t.annotations.open_world_hint for t in tools.tools)
            caps = unpack(await client.call_tool("list_data_capabilities", {}))
            assert caps["status"] == "ok"
            assert caps["data"]["services"]
            assert "credential" not in json.dumps(caps)
            selected = unpack(await client.call_tool("list_data_capabilities", {"service_id": "molit-apartment-sale"}))
            assert len(selected["data"]["services"]) == 1
            assert unpack(await client.call_tool("list_data_capabilities", {"service_id": "absent"}))["status"] == "no_data"
            first = unpack(await client.call_tool("search_transactions", {"limit": 1}))
            assert first["status"] == "ok"
            assert first["data"]["rows"][0]["building_name"] == "한글 아파트"
            assert first["data"]["total"] == 2
            assert first["excluded"]["count"] == 1
            assert first["provenance"]["release_id"] == "synthetic"
            assert first["provenance"]["manifest_sha256"]
            assert first["snapshot_refs"][0]["commit_sha"] == "b" * 40
            assert first["coverage"]["release"]["months"] == ["2026-10"]
            assert first["warnings"] and first["unresolved_gaps"]
            assert first["legal_verified"] is False
            second = unpack(await client.call_tool("search_transactions", {"limit": 1, "cursor": first["next_cursor"]}))
            assert second["data"]["rows"][0]["record_version_id"] == "2"
            assert second["next_cursor"] is None
            for args in ({"filters": {"sql": "SELECT 1"}}, {"limit": 1001}, {"limit": 0},
                         {"filters": {"area_m2_min": "bad"}}, {"cursor": "bad"},
                         {"filters": {"include_cancelled": "yes"}}):
                result = unpack(await client.call_tool("search_transactions", args))
                assert result["status"] == "error" and result["data"]["error_code"] == "invalid_query"
            empty = unpack(await client.call_tool("search_transactions", {"filters": {"lawd_code": "99999"}}))
            assert empty["status"] == "no_data" and empty["data"]["total"] == 0
            active.active_manifest.unlink()
            assert unpack(await client.call_tool("search_transactions", {}))["status"] == "no_active_release"
    asyncio.run(exercise())


def test_stdio_protocol(active, tmp_path):
    config = tmp_path / "runtime.json"
    config.write_text(json.dumps({"workspace": ".", "data_root": "data"}), encoding="utf-8")
    async def exercise():
        params = StdioServerParameters(command=sys.executable,
            args=["-I", "-B", "-m", "kapa_runtime.mcp.server"],
            env={"KAPA_RUNTIME_CONFIG": str(config)})
        async with Client(params, mode="legacy", read_timeout_seconds=60) as client:
            assert len((await client.list_tools()).tools) == 3
            assert unpack(await client.call_tool("list_data_capabilities", {}))["status"] == "ok"
            assert unpack(await client.call_tool("search_transactions", {}))["data"]["total"] == 2
    asyncio.run(exercise())


def test_import_and_calls_never_load_credentials_or_hub(active, tmp_path):
    code = '''import sys
def no_external_access(event, args):
    if event in {"socket.connect", "subprocess.Popen", "os.system"}:
        raise AssertionError("network and subprocess access forbidden")
    if event == "open":
        path = str(args[0]).replace(chr(92), "/")
        if ".git" in path.split("/") or path.endswith("/deps.lock"):
            raise AssertionError("workspace provenance reads forbidden")
from pathlib import Path
from kapa_runtime.config import RuntimeSettings
from kapa_runtime.mcp.server import create_server
from mcp import Client
import asyncio
# Windows asyncio creates a loopback socketpair while constructing its loop.
loop = asyncio.new_event_loop()
sys.addaudithook(no_external_access)
import json
async def run():
    async with Client(create_server(RuntimeSettings(Path(sys.argv[1]), Path(sys.argv[2]))), mode="legacy") as c:
        for tool in ("list_data_capabilities", "search_transactions", "get_data_coverage"):
            result = await c.call_tool(tool, {})
            assert not result.is_error
            assert json.loads(result.content[0].text)["status"] == "ok"
try:
    loop.run_until_complete(run())
finally:
    loop.close()
assert not any(n == "kapa_runtime.credentials" or n.startswith("kapa_runtime.credentials.") or n == "huggingface_hub" or n.startswith("huggingface_hub.") for n in sys.modules)
'''
    completed = subprocess.run([sys.executable, "-I", "-B", "-c", code,
        str(active.workspace), str(active.data_root)], capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize("tamper", ["manifest", "bytes", "extra", "ledger"])
def test_fail_closed(active, tamper):
    if tamper == "manifest":
        value = json.loads(active.active_manifest.read_text())
        value["coverage"]["months"] = ["2000-01"]
        active.active_manifest.write_text(json.dumps(value), encoding="utf-8")
    elif tamper == "bytes":
        next(active.snapshots_dir.rglob("*.parquet")).write_bytes(b"tampered")
    elif tamper == "extra":
        (active.snapshots_dir / "synthetic" / "extra.parquet").write_bytes(b"extra")
    else:
        active.state_db.unlink()
    before = {p.relative_to(active.data_root): p.read_bytes() for p in active.data_root.rglob("*") if p.is_file()}
    async def exercise():
        async with Client(create_server(active), mode="legacy") as client:
            result = unpack(await client.call_tool("search_transactions", {}))
            assert result["status"] == "error"
            assert result["data"] == {"error_code": "active_snapshot_unverified"}
    asyncio.run(exercise())
    after = {p.relative_to(active.data_root): p.read_bytes() for p in active.data_root.rglob("*") if p.is_file()}
    assert before == after


def test_v2_search_metadata_is_bounded(tmp_path):
    from test_cumulative_query import active_fixture
    settings, _ = active_fixture(tmp_path, pairs=[(str(10000 + i), '202510') for i in range(60)])

    async def exercise():
        async with Client(create_server(settings), mode="legacy") as client:
            result = unpack(await client.call_tool('search_transactions', {'limit': 1}))
            assert result['status'] == 'ok'
            assert set(result['data']) == {'rows', 'total'}
            assert result['data']['total'] == 120
            assert len(result['partition_provenance']) == 1
            assert result['partition_provenance'][0]['observation_id'] == result['data']['rows'][0]['observation_id']
            assert result['partition_provenance_summary']['truncated'] is False
            assert set(result['coverage']['query']) == {'scope', 'details'}
            assert len(json.dumps(result, ensure_ascii=False).encode()) < 8000
    asyncio.run(exercise())


def test_v2_coverage_no_matching_key(tmp_path):
    from test_cumulative_query import active_fixture
    settings, _ = active_fixture(tmp_path)

    async def exercise():
        async with Client(create_server(settings), mode="legacy") as client:
            result = unpack(await client.call_tool('get_data_coverage', {'filters': {'lawd_code': '99999'}}))
            assert result['status'] == 'no_data'
            assert result['data'] == {'rows': [], 'total': 0}
            assert result['next_cursor'] is None
            assert result['partition_provenance'] == []
            # A declared but uncollected key is coverage, not no_data.
            gap = unpack(await client.call_tool('get_data_coverage', {'filters': {'lawd_code': '41110'}}))
            assert gap['status'] == 'ok'
            assert gap['coverage']['query']['not_collected']
    asyncio.run(exercise())


@pytest.mark.parametrize('stage', ['initial', 'dataset', 'post_scan', 'constructor'])
@pytest.mark.parametrize('tool', ['search_transactions', 'get_data_coverage'])
def test_mid_query_integrity_is_not_invalid_query(tmp_path, monkeypatch, stage, tool):
    from test_cumulative_query import active_fixture
    from kapa_runtime.storage import active as module
    from kapa_runtime.storage.snapshot_query import SnapshotInventory
    settings, manifest = active_fixture(tmp_path)
    victim = settings.snapshots_dir / 'A' / manifest['selected_partitions'][0]['files'][0]['path']
    if stage in ('dataset', 'post_scan') and tool == 'get_data_coverage':
        pytest.skip('coverage does not scan transaction datasets')
    if stage == 'initial':
        original = module.verify_local
        def changed(*args, **kwargs):
            original(*args, **kwargs)
            victim.write_bytes(b'tampered')
        monkeypatch.setattr(module, 'verify_local', changed)
    elif stage == 'constructor':
        original = module.TransactionQuery
        def changed(*args, **kwargs):
            victim.write_bytes(b'tampered')
            raise ValueError('metadata changed before constructing reader')
        monkeypatch.setattr(module, 'TransactionQuery', changed)
    else:
        original = SnapshotInventory.dataset
        def changed(*args, **kwargs):
            dataset = original(*args, **kwargs)
            if stage == 'dataset':
                victim.write_bytes(b'tampered')
            else:
                # Valid Parquet still reads, but bytes no longer match its digest.
                table = pq.read_table(victim, partitioning=None)
                pq.write_table(table, victim, compression='NONE')
            return dataset
        import pyarrow.parquet as pq
        monkeypatch.setattr(SnapshotInventory, 'dataset', changed)

    async def exercise():
        async with Client(create_server(settings), mode="legacy") as client:
            result = unpack(await client.call_tool(tool, {}))
            assert result['status'] == 'error'
            assert result['data'] == {'error_code': 'active_snapshot_unverified'}
    asyncio.run(exercise())


@pytest.mark.parametrize('tool', ['search_transactions', 'get_data_coverage'])
def test_shard_changed_after_initial_verification(tmp_path, monkeypatch, tool):
    from test_cumulative_query import active_fixture
    from kapa_runtime.releases.manifest_v2 import coverage_for_partitions
    from kapa_runtime.storage import active as module
    settings, manifest = active_fixture(tmp_path)
    shard = {'schema_version': 'kapa-runtime-coverage/1',
             'license': 'SPDX-License-Identifier: GPL-3.0-only',
             'selected_partitions': manifest['selected_partitions'], 'coverage': manifest['coverage']}
    payload = json.dumps(shard).encode()
    digest = hashlib.sha256(payload).hexdigest()
    name = f'manifests/coverage/{digest}.json'
    victim = settings.snapshots_dir / 'A' / name
    victim.parent.mkdir(parents=True)
    victim.write_bytes(payload)
    count = len(shard['selected_partitions']) + sum(len(shard['coverage'][n])
        for n in ('declared', 'observed', 'zero', 'not_collected'))
    manifest.update(selected_partitions=[], coverage=coverage_for_partitions([], []),
                    coverage_shards=[{'path': name, 'sha256': digest, 'entries': count}])
    dump_manifest(manifest, settings.active_manifest)
    with sqlite3.connect(settings.state_db) as con:
        con.execute('UPDATE releases SET guard_evidence=?', (json.dumps([
            {'state': 'published', 'evidence': {'commit_reread': True,
             'published_manifest_sha256': manifest_digest(manifest)}}]),))
    original = module.verify_local
    def changed(*args, **kwargs):
        original(*args, **kwargs)
        victim.write_bytes(payload + b' ')
    monkeypatch.setattr(module, 'verify_local', changed)

    async def exercise():
        async with Client(create_server(settings), mode='legacy') as client:
            result = unpack(await client.call_tool(tool, {}))
            assert result['status'] == 'error'
            assert result['data'] == {'error_code': 'active_snapshot_unverified'}
    asyncio.run(exercise())
