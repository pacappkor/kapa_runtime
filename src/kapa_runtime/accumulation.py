# SPDX-License-Identifier: GPL-3.0-only
"""Opt-in, local-only pair jobs. No publisher or ACTIVE mutation lives here."""
from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime
import hashlib
import json
from pathlib import Path
import tempfile
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx

from . import pipeline as p
from .adapters import molit_rtms as adapter
from .normalization.rtms import normalize
from .planning import Budget, ServiceRange, plan_day
from .registry import ServiceRegistry
from .releases.hf import scan_secret_payload
from .releases.manifest import build_manifest, dumps_manifest, manifest_digest, relative_path
from .releases.manifest_v2 import bounded_strings, resolve_snapshot
from .releases.snapshot import build_snapshot, check_parent, materialize, preflight_selection, write_once
from .storage.metadata import read_metadata
from .storage.parquet import _safe_path, write_partition
from .storage.state import StateStore, _dump

SERVICES = p.SERVICES
MAX_BATCH_BYTES = 512 * 1024 * 1024


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str,
                                     ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def lawd_reference(exclude=True):
    """Provisional RTMS rule: city name prefix + numeric sub-gu sibling.

    Names distinguish a city from unrelated codes sharing four digits. The
    committed reference is unmodified; both source and provenance bind the job.
    """
    directory = Path(__file__).parent / "registry"
    raw = (directory / "lawd_codes.json").read_bytes()
    provenance = (directory / "lawd_codes.provenance.json").read_bytes()
    entries = json.loads(raw)["codes"]
    active = [v for v in entries if v["active"]]
    parents = {v["code"] for v in active if v["code"].endswith("0")
               and v["name"].endswith("시") and any(
                   child["code"] != v["code"]
                   and child["code"][:4] == v["code"][:4]
                   and child["name"].startswith(v["name"] + " ")
                   and child["name"].endswith("구") for child in active)}
    return [v for v in active if not exclude or v["code"] not in parents], digest(
        [hashlib.sha256(raw).hexdigest(), hashlib.sha256(provenance).hexdigest()])


def _day(clock):
    now = clock()
    p._require(now.utcoffset() is not None, "invalid_clock")
    return now.astimezone(ZoneInfo("Asia/Seoul")).date().isoformat()


def _load(root, name):
    path = root / relative_path(name)
    _safe_path(path)
    return path.read_bytes()


def _parent(settings, store):
    parent = p._read_manifest(settings.active_manifest)
    check_parent(store, {"release_id": parent["release_id"], "digest": manifest_digest(parent)},
                 active_path=settings.active_manifest)
    root = settings.snapshots_dir / parent["release_id"]
    p._verify_local(root, parent)
    # Legacy bootstrap requires explicitly attested partition evidence (step 6).
    p._require(parent["schema_version"] == "kapa-runtime-release/2", "bootstrap_required")
    resolved = resolve_snapshot(parent, lambda name: read_metadata(root / relative_path(name), as_bytes=True))
    return parent, resolved, root


def _coverage(resolved, store):
    coverage = {}
    for part in resolved["selected_partitions"]:
        key = part["lawd_code"], part["deal_ymd"]
        stamp = datetime.fromisoformat(part["fetched_at"]).astimezone(ZoneInfo("Asia/Seoul")).date()
        coverage[key] = min(stamp, coverage.get(key, stamp))
    for row in store._connection.execute("SELECT job_id, lawd_code, deal_ymd FROM work_items WHERE state='complete'"):
        parts = _completed(store, row[0], row[1], row[2])
        if len(parts) == 2:
            stamp = min(datetime.fromisoformat(v["partition"]["fetched_at"]).astimezone(
                ZoneInfo("Asia/Seoul")).date() for v in parts.values())
            key = row[1], row[2]
            coverage[key] = max(stamp, coverage.get(key, stamp))
    return coverage


def _plan(settings, store, resolved, policy, clock):
    codes, reference = lawd_reference(settings.exclude_parent_city_aggregates)
    day = _day(clock)
    budgets = [policy.get("services", {}).get(s, {}).get("max_requests") for s in SERVICES]
    p._require(all(type(v) is int and 3 <= v <= 150 and v % 3 == 0 for v in budgets), "invalid_pair_budget")
    limits = []
    registry = ServiceRegistry.load()
    for service in SERVICES:
        limit = policy.get("services", {}).get(service, {}).get("daily_limit", 9000)
        p._require(type(limit) is int and limit >= policy['services'][service]['max_requests'],
                   "invalid_daily_quota")
        limits.append(min(9000, limit, registry.get(service).quota["daily_limit"]))
    budget = Budget(sale_requests=max(0, limits[0] - store.reserved_requests(SERVICES[0], day)),
                    rent_requests=max(0, limits[1] - store.reserved_requests(SERVICES[1], day)),
                    requests_per_pair=budgets[0],
                    sale_requests_per_pair=budgets[0], rent_requests_per_pair=budgets[1])
    scope = policy.get("accumulation_scope")
    ranges = {}
    if scope is not None:
        p._require(isinstance(scope, dict) and set(scope) == {"lawd_codes", "start", "end", "batch_cap"},
                   "invalid_accumulation_scope")
        selected = scope["lawd_codes"]
        p._require(isinstance(selected, list) and bool(selected) and
                   all(isinstance(code, str) for code in selected) and
                   len(set(selected)) == len(selected) and
                   set(selected) <= {v["code"] for v in codes}, "invalid_accumulation_scope")
        p._require(type(scope["batch_cap"]) is int and 1 <= scope["batch_cap"] <= 60,
                   "invalid_accumulation_scope")
        from .planning import _month
        try:
            p._require(_month(scope["start"]) <= _month(scope["end"]), "invalid_accumulation_scope")
        except ValueError:
            raise p.PipelineError("invalid_accumulation_scope") from None
        codes = [v for v in codes if v["code"] in selected]
        budget = replace(budget, batch_cap=scope["batch_cap"])
        for service, history_start in (("sale", "200601"), ("rent", "201101")):
            start = max(scope["start"], history_start)
            end = min(scope["end"], day[:7].replace("-", ""))
            p._require(start <= end, "accumulation_scope_empty_service_range")
            ranges[service] = ServiceRange(start, end)
    coverage = _coverage(resolved, store)
    plan = plan_day(codes, today=datetime.fromisoformat(day).date(), coverage=coverage, budget=budget, **ranges)
    if not plan.items and budget.pair_capacity == 0:
        # Persist real work, not an empty terminal job, so tomorrow can resume it.
        plan = plan_day(codes, today=datetime.fromisoformat(day).date(), coverage=coverage,
                        budget=Budget(requests_per_pair=budget.requests_per_pair, batch_cap=1), **ranges)
    return plan, budget, reference


def plan(settings, *, clock=lambda: datetime.now(ZoneInfo("Asia/Seoul"))):
    """No network/credentials; takes the same lock for a coherent ledger view."""
    with StateStore(settings.state_db) as store:
        store.migrate_jobs()
        _, resolved, _ = _parent(settings, store)
        policy = p._policy(settings)
        daily, budget, _ = _plan(settings, store, resolved, policy, clock)
        return {"state": "planned", "plan": asdict(daily),
                "reservations_per_service": (daily.reserved_requests_per_service
                    if len({policy["services"][s]["max_requests"] for s in SERVICES}) == 1
                    else {s: len(daily.items) * policy["services"][s]["max_requests"] for s in SERVICES}),
                "requests_per_attempt": {s: policy["services"][s]["max_requests"] for s in SERVICES}}


def _completed(store, job, code, month):
    return {row[0]: json.loads(row[1]) for row in store._connection.execute(
        "SELECT service_id,data FROM attempts WHERE job_id=? AND lawd_code=? AND deal_ymd=? AND state='complete'",
        (job, code, month))}


def _reserve(store, job, code, month, amount, day, limits=None):
    """Both services or neither, using the existing runs daily reservation ledger."""
    limits = limits or {s: 9000 for s in SERVICES}
    amounts = amount if isinstance(amount, dict) else {s: amount for s in SERVICES}
    p._require(all(store.reserved_requests(s, day) + amounts[s] <= limits[s] for s in SERVICES), "quota_pause")
    con = store._connection
    con.execute("BEGIN IMMEDIATE")
    try:
        for service in SERVICES:
            store.record_run(uuid4().hex, service_id=service, quota_day=day,
                             reserved_requests=amounts[service], daily_limit=limits[service],
                             job_id=job, lawd_code=code, deal_ymd=month)
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise


def _verify_result(settings, evidence, code, month, secrets):
    root = settings.data_root / "accumulation"
    service = evidence["partition"]["service_id"]
    completed = evidence["completed"]
    bound = {key: evidence[key] for key in ("pages", "payload_sha256", "parser_version")}
    p._require(digest(bound) == evidence["partition"]["evidence_digest"]
               and evidence["parser_version"] == adapter.PARSER_VERSION, "verification_failed")
    # Replay normalization with the original raw pages, then check typed bytes.
    files = completed["files"]
    from .storage.parquet import read_partition, schema_for
    import pyarrow.parquet as pq
    for entry in files:
        path = root / relative_path(entry["path"])
        _safe_path(path)
        p._require(p._hash(path) == entry["sha256"], "verification_failed")
        p._require(pq.read_schema(path).equals(schema_for("kapa-runtime-data/2"), check_metadata=True), "schema_failed")
        p._require(len(read_partition(path)) == entry["rows"], "verification_failed")
        p._secret_check(path, secrets)
    pages, totals, sizes = [], [], []
    payload_hash = hashlib.sha256()
    from .credentials import Secret
    for page in evidence["pages"]:
        payload = _load(root, page["path"])
        p._require(hashlib.sha256(payload).hexdigest() == page["sha256"], "verification_failed")
        scan_secret_payload(payload, "page.xml", secrets)
        payload_hash.update(payload)
        parsed, total, size = adapter._parse(payload, Secret("replay-sentinel"))
        p._require(parsed.page_no == len(pages) + 1, "verification_failed")
        pages.append(parsed)
        totals.append(total)
        sizes.append(size)
    p._require(len(pages) == completed["pages_received"] == completed["pages_expected"]
               and payload_hash.hexdigest() == evidence["payload_sha256"]
               and bool(pages) and len(set(totals)) == len(set(sizes)) == 1
               and sum(len(v.items) for v in pages) == totals[0]
               and (completed["status"] == "zero") == (totals[0] == 0), "verification_failed")
    from types import SimpleNamespace
    observation = SimpleNamespace(service_id=service, observation_id=completed["observation_id"],
        request={"LAWD_CD": code}, fetched_at=datetime.fromisoformat(completed["fetched_at"]))
    expected = normalize(SimpleNamespace(observation=observation, pages=pages),
                         transaction_type=service.rsplit("-", 1)[1])
    actual = [r for f in files for r in read_partition(root / f["path"])]
    order = lambda r: (r.record_version_id, r.source_payload_ref)
    p._require(sorted(expected, key=order) == sorted(actual, key=order), "verification_failed")


def _save_result(settings, store, result, job, code, month, attempt, secrets):
    obs = result.observation
    p._check_identifiers(secrets, obs.observation_id, obs.run_id, obs.service_id,
                         job, code, month, attempt)
    p._id(obs.observation_id)
    p._require(obs.service_id in SERVICES and obs.request["LAWD_CD"] == code
               and obs.request["DEAL_YMD"] == month and obs.parser_version == adapter.PARSER_VERSION,
               "schema_failed")
    p._require(hashlib.sha256(b"".join(page.sanitized for page in result.pages)).hexdigest()
               == obs.sanitized_payload_sha256, "verification_failed")
    store.record_observation(obs)
    store._connection.execute("UPDATE attempts SET observation_id=? WHERE attempt_id=?",
                              (obs.observation_id, attempt))
    root = settings.data_root / "accumulation"
    pages = []
    for page in result.pages:
        scan_secret_payload(page.sanitized, "page.xml", secrets)
        name = f"data/observations/{obs.observation_id}/raw/page-{page.page_no:05d}.xml"
        write_once(root, name, page.sanitized, page.sha256)
        pages.append({"path": name, "sha256": page.sha256, "page": page.page_no})
    evidence = {"pages": pages, "payload_sha256": obs.sanitized_payload_sha256,
                "parser_version": obs.parser_version}
    store._connection.execute("UPDATE attempts SET observation_id=?,state=?,data=? WHERE attempt_id=?",
                              (obs.observation_id, str(obs.status), _dump(evidence), attempt))
    if str(obs.status) not in ("success", "zero"):
        warnings = set(obs.warnings)
        if "authentication_error" in warnings:
            raise p.PipelineError("auth_blocked")
        if "page_budget_exhausted" in warnings:
            raise p.PipelineError("partition_blocked")
        if warnings.intersection({"retry_exhausted", "provider_error", "quota_exceeded"}):
            raise p.PipelineError("provider_pause")
        raise p.PipelineError("schema_blocked")
    p._require(obs.coverage.get("complete") is True and obs.pages_received == obs.pages_expected
               == len(pages) and [v.page_no for v in result.pages] == list(range(1, len(pages) + 1)),
               "verification_failed")
    normalization_warnings = []
    records = normalize(result, transaction_type=obs.service_id.rsplit("-", 1)[1],
                        warnings=normalization_warnings)
    p._require(not normalization_warnings, "schema_blocked")
    p._require(len(records) == obs.count and (str(obs.status) == "zero") == (len(records) == 0),
               "verification_failed")
    _safe_path(root)
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as temporary:
        scratch = Path(temporary)
        written = write_partition(records, scratch, asset_type="apartment",
            transaction_type=obs.service_id.rsplit("-", 1)[1], lawd_code=code,
            contract_year=int(month[:4]), contract_month=int(month[4:]))
        files = []
        for file in written:
            p._secret_check(file.path, secrets)
            name = f"data/observations/{obs.observation_id}/" + file.path.relative_to(scratch / "data").as_posix()
            write_once(root, name, file.path.read_bytes(), file.sha256)
            files.append({"path": name, "sha256": file.sha256, "rows": file.rows})
    part = dict(service_id=obs.service_id, lawd_code=code, deal_ymd=month,
        producer_release_id=job, origin_digest=None, observation_id=obs.observation_id,
        run_id=obs.run_id, fetched_at=obs.fetched_at.isoformat(), status=str(obs.status),
        rows=len(records), parser_version=obs.parser_version, schema="kapa-runtime-data/2",
        previous_observation=None, evidence_digest=digest(evidence), files=files)
    evidence.update(partition=part, completed={**part, "pages_received": obs.pages_received,
                                              "pages_expected": obs.pages_expected})
    _verify_result(settings, evidence, code, month, secrets)
    store._connection.execute("UPDATE attempts SET state='complete',data=? WHERE attempt_id=?",
                              (_dump(evidence), attempt))
    return evidence


def _terms(policy):
    """Apply the build path's reviewed, nonblank string evidence contract."""
    terms = {s: policy.get("services", {}).get(s, {}) for s in SERVICES}
    p._require(all(t.get("terms_reviewed") is True
                   and isinstance(t.get("terms_basis"), str)
                   and bool(t["terms_basis"].strip()) for t in terms.values()),
               "build_evidence_required")
    # Service dictionaries are copied intact into quality.terms_evidence.
    # Reject oversized extensible metadata before reserving quota or collecting.
    try:
        bounded_strings(terms)
    except ValueError:
        raise p.PipelineError("build_evidence_required") from None
    return terms


def _preflight(parent, resolved, items, policy):
    """Conservative metadata bounds before reserving quota or calling an adapter.

    Generated partitions have one file and UUID identifiers; 16 KiB per pair
    includes both partitions, coverage and predecessor evidence. Existing blocks
    are split if necessary, so root growth is bounded by explicit references.
    """
    from .releases import manifest_v2 as v2
    from .releases.manifest import _json
    pairs = {(p['lawd_code'], p['deal_ymd']) for p in items}
    known = {(p['lawd_code'], p['deal_ymd']) for p in
             resolved['coverage']['declared'] + resolved['selected_partitions']}
    count = (len(known | pairs) * 8)
    # Worst case: every touched block splits into pair-sized shards. Untouched
    # refs stay fixed. This intentionally refuses near-limit uncertain jobs.
    touched_entries = 0
    for part in resolved['selected_partitions']:
        if (part['lawd_code'], part['deal_ymd']) in pairs:
            touched_entries += len(_json(part).encode())
    # Bound splitting by both entry count and serialized bytes. A binary split
    # can leave two half-full blocks; retain that headroom in the estimate.
    inherited_bytes = sum(len(_json(part).encode()) + 1024
                          for part in resolved['selected_partitions'])
    refs = (len(parent.get('coverage_shards', [])) + len(pairs) * 2 + 1
            + 2 * ((count + v2.MAX_SHARD_ENTRIES - 1) // v2.MAX_SHARD_ENTRIES)
            + 2 * ((inherited_bytes + v2.MAX_METADATA_BYTES - 1) // v2.MAX_METADATA_BYTES))
    root_bytes = len(_json({**parent, 'selected_partitions': [], 'coverage': {}}).encode())
    pair_bytes = {}
    for part in resolved['selected_partitions']:
        identity = (part['lawd_code'], part['deal_ymd'])
        pair_bytes[identity] = pair_bytes.get(identity, 0) + len(_json(part).encode()) + 2048
    p._require(all(size + 16384 <= v2.MAX_METADATA_BYTES for size in pair_bytes.values()),
               'manifest_capacity_exceeded')
    producers = {part['producer_release_id'] for part in resolved['selected_partitions']}
    root_bytes += len(_json(policy).encode()) + refs * 300 + len(producers) * 300 + 65536
    p._require(count <= v2.MAX_SNAPSHOT_ENTRIES and refs <= v2.MAX_SHARDS
               and root_bytes <= v2.MAX_METADATA_BYTES
               and (not pairs or v2.MAX_METADATA_BYTES >= 16384)
               and touched_entries <= v2.MAX_METADATA_BYTES * max(1, len(pairs)),
               'manifest_capacity_exceeded')


def _new_job_id(settings, store, parent, job):
    collision = job == parent['release_id']
    for table in ('releases', 'snapshot_parents', 'batch_members', 'partition_selections', 'public_promotions'):
        collision |= store._connection.execute(
            f'SELECT 1 FROM {table} WHERE release_id=? LIMIT 1', (job,)).fetchone() is not None
    for directory, name in ((settings.manifests_dir, job + '.json'),
                            (settings.snapshots_dir, job), (settings.staging_dir, job)):
        path = directory / name
        try:
            path.lstat()
        except FileNotFoundError:
            pass
        else:
            collision = True
        p._require(not collision, 'job_id_collision')
        _safe_path(path)
    p._require(not collision, 'job_id_collision')


def collect_batch(settings, *, job_id=None, dry_run=False, client=None,
                  clock=lambda: datetime.now(ZoneInfo("Asia/Seoul"))):
    if dry_run:
        return plan(settings, clock=clock)
    policy = p._policy(settings)
    p._require(policy.get("accumulation_smoke_verified") is True, "operator_smoke_required")
    _terms(policy)
    secrets = p._loaded_secrets(settings)
    # Check before opening the ledger or deriving any path from operator input.
    job = job_id if job_id is not None else "job-" + uuid4().hex
    try:
        p._check_identifiers(secrets, job)
    except Exception:
        raise p.PipelineError("identifier_scan_failed") from None
    job = p._id(job)
    with StateStore(settings.state_db) as store:
        store.migrate_jobs()
        con = store._connection
        # Persistent operator intervention gate; changing the date/job ID cannot
        # silently retry authentication, schema or security failures.
        p._require(not con.execute("SELECT 1 FROM collection_jobs WHERE state='blocked'").fetchone(),
                   "operator_intervention_required")
        parent, resolved, parent_root = _parent(settings, store)
        row = con.execute("SELECT state,data FROM collection_jobs WHERE job_id=?", (job,)).fetchone()
        daily, budget, reference = _plan(settings, store, resolved, policy, clock)
        binding = {"reference_digest": reference, "policy_digest": digest(policy),
                   "parser_version": adapter.PARSER_VERSION,
                   "exclude_parent_city_aggregates": settings.exclude_parent_city_aggregates,
                   "parent_digest": manifest_digest(parent)}
        if row:
            data = json.loads(row[1])
            p._require(all(data.get(k) == v for k, v in binding.items()), "job_binding_changed")
            if row[0] == "built":
                manifest = p._read_manifest(settings.manifests_dir / (job + ".json"))
                p._require(manifest.get("quality", {}).get("plan_digest") == data["plan_digest"]
                           and manifest.get("quality", {}).get("policy_digest") == data["policy_digest"],
                           "verification_failed")
                store.record_snapshot(manifest, loader=lambda name: _load(settings.snapshots_dir / job, name))
                p._verify_local(settings.snapshots_dir / job, manifest)
                return {"state": "validated", "pairs": len(manifest["batch_scope"]["declared_pairs"])}
        else:
            _new_job_id(settings, store, parent, job)
            _preflight(parent, resolved, [asdict(item) for item in daily.items], policy)
            # Do not strand a completed unpublished batch by starting another job.
            p._require(not con.execute("SELECT 1 FROM collection_jobs WHERE state IN ('preparing','running','paused','built')").fetchone(),
                       "resume_existing_job_required")
            data = {**binding, "plan": asdict(daily), "plan_digest": digest(asdict(daily)),
                    "requests_per_attempt": {s: policy["services"][s]["max_requests"] for s in SERVICES}}
            con.execute("BEGIN IMMEDIATE")
            try:
                con.execute("INSERT INTO collection_jobs VALUES (?,'running',?)", (job, _dump(data)))
                for item in daily.items:
                    con.execute("INSERT INTO work_items VALUES (?,?,?,'pending')", (job, *item.pair))
                con.execute("COMMIT")
            except BaseException:
                con.execute("ROLLBACK")
                raise
        _preflight(parent, resolved, data['plan']['items'], policy)
        preflight_selection(parent, resolved, data['plan']['items'], lambda name: _load(parent_root, name))
        for path in (settings.manifests_dir / (job + '.json'), settings.snapshots_dir / job,
                     settings.staging_dir / job, settings.data_root / 'accumulation'):
            _safe_path(path)
        amount = {s: policy["services"][s]["max_requests"] for s in SERVICES}
        try:
            registry = ServiceRegistry.load()
            limits = {}
            for service in SERVICES:
                configured = policy.get("services", {}).get(service, {}).get("daily_limit", 9000)
                p._require(type(configured) is int and configured > 0, "invalid_daily_quota")
                limits[service] = min(9000, configured, registry.get(service).quota["daily_limit"])
            p._require(digest(data["plan"]) == data["plan_digest"], "verification_failed")
            con.execute("UPDATE attempts SET state='interrupted' WHERE job_id=? AND state='running'", (job,))
            for item in data["plan"]["items"]:
                code, month = item["lawd_code"], item["deal_ymd"]
                completed = _completed(store, job, code, month)
                for evidence in completed.values():
                    _verify_result(settings, evidence, code, month, secrets)
                if len(completed) == 2:
                    con.execute("UPDATE work_items SET state='complete' WHERE job_id=? AND lawd_code=? AND deal_ymd=?",
                                (job, code, month))
                    continue
                days = set()
                def reserve():
                    day = _day(clock)
                    if day not in days:
                        _reserve(store, job, code, month, amount, day, limits)
                        days.add(day)
                reserve()  # Both services before either request, also on rollover.
                for service in SERVICES:
                    if service in completed:
                        continue
                    spec = registry.get(service)
                    p._require(spec.operation_verified and spec.approval_status.startswith("connection-verified-"),
                               "schema_blocked")
                    secret = p.load_secret(spec.credential_alias, settings)
                    run = uuid4().hex
                    attempt = uuid4().hex
                    store.record_run(run, job_id=job, service_id=service)
                    con.execute("INSERT INTO attempts VALUES (?,?,?,?,?,?,NULL,'running','{}')",
                                (attempt, job, code, month, service, run))
                    def perform(http):
                        return adapter.collect(spec, secret, lawd_code=code, deal_ymd=month,
                            client=http, run_id=run, max_pages=amount[service] // 3, before_request=reserve, stop_on_quota=True)
                    if client is None:
                        with httpx.Client(timeout=30, follow_redirects=False, trust_env=False) as http:
                            result = perform(http)
                    else:
                        result = perform(client)
                    p._require(result.observation.run_id == run and result.observation.service_id == service,
                               "verification_failed")
                    completed[service] = _save_result(settings, store, result, job, code, month, attempt, secrets)
                con.execute("UPDATE work_items SET state='complete' WHERE job_id=? AND lawd_code=? AND deal_ymd=?",
                            (job, code, month))
            return _freeze(settings, store, job, data, parent, resolved, parent_root, secrets, policy)
        except Exception as exc:
            code = exc.error_code if isinstance(exc, p.PipelineError) else "verification_failed"
            pause = code in ("quota_pause", "provider_pause")
            con.execute("UPDATE attempts SET state='interrupted' WHERE job_id=? AND state='running'", (job,))
            data["stop_code"] = code
            con.execute("UPDATE collection_jobs SET state=?,data=? WHERE job_id=?",
                        ("paused" if pause else "blocked", _dump(data), job))
            return {"state": "paused" if pause else "blocked", "error_code": code,
                    "completed_pairs": con.execute("SELECT count(*) FROM work_items WHERE job_id=? AND state='complete'", (job,)).fetchone()[0]}


def _freeze(settings, store, job, data, parent, resolved, parent_root, secrets, policy):
    parts = []
    for item in data["plan"]["items"]:
        completed = _completed(store, job, item["lawd_code"], item["deal_ymd"])
        p._require(len(completed) == 2, "incomplete_pair")
        parts.extend(v["partition"] for v in completed.values())
    parts.sort(key=lambda part: (part["lawd_code"], part["deal_ymd"], part["service_id"]))
    if not parts:
        store._connection.execute("UPDATE collection_jobs SET state='empty' WHERE job_id=?", (job,))
        return {"state": "empty", "pairs": 0}
    root = settings.data_root / "accumulation"
    total = sum((root / f["path"]).stat().st_size for part in parts for f in part["files"])
    p._require(len(parts) <= 120 and total <= MAX_BATCH_BYTES, "batch_cap_exceeded")
    sha = p._git_revision(settings)
    siblings, warnings = p.workspace_guard.capture(settings)
    terms = _terms(policy)
    p._require(policy.get("fixture_evidence", {}).get("passed") is True
               and policy["fixture_evidence"].get("github_revision") == sha, "build_evidence_required")
    template = build_manifest(release_id=job, state="validated", files=[],
        created_at=str(data["plan"]["day"]) + "T00:00:00+09:00",
        parser_version=adapter.PARSER_VERSION, source_runs=sorted(v["run_id"] for v in parts),
        github_revisions={"pacappkor/kapa_runtime": sha, **siblings},
        quality={"passed": True, "legal_verified": False, "fixture_revision": sha,
                 "plan_digest": data["plan_digest"], "reference_digest": data["reference_digest"],
                 "policy_digest": data["policy_digest"], "terms_evidence": terms})
    origins = {parent["release_id"]: parent}
    for part in resolved["selected_partitions"]:
        rid = part["producer_release_id"]
        if rid not in origins:
            origins[rid] = p._read_manifest(settings.manifests_dir / (rid + ".json"))
    # Keep original roots for hash-bound shard resolution across producer manifests.
    def load_origin(name):
        for rid in origins:
            path = settings.snapshots_dir / rid / relative_path(name)
            _safe_path(path)
            if path.exists():
                return read_metadata(path, as_bytes=True)
        raise p.PipelineError("origin_missing")
    removed = {event["evidence"].get("removed_path") for row in store.list_releases()
               for event in row["guard_evidence"]
               if event["evidence"].get("reason") == "retained_partition_removed"}
    shard_payloads = {}
    manifest = build_snapshot(store, template, parent, parts, active_path=settings.active_manifest,
                              origins=origins, loader=load_origin, tombstones=removed,
                              shard_payloads=shard_payloads, enabled=True)
    # Freeze a manifest-aware default config; archive observations are never
    # included by a wildcard in the public dataset's default view.
    from .releases.manifest_v2 import resolve_snapshot_inventory
    entries = resolve_snapshot_inventory(manifest, lambda name: shard_payloads.get(name) or load_origin(name))
    paths = [f['path'] for f in entries if f['path'].endswith('.parquet')]
    card = ('---\nlanguage: [ko]\nconfigs:\n- config_name: default\n  data_files:\n'
            '  - split: train\n    path:\n' + ''.join('    - ' + json.dumps(name) + '\n' for name in paths)
            + '---\n# Cumulative market observations\n\n'
            'Only selected observations are included in the default view. Historical observations '
            'are retained, not duplicate current records. Use manifests/release.json for exact '
            'coverage and provenance. Partial addresses; cancellations are preserved. '
            'legal_verified=false. Raw responses and logs are not published.\n')
    card_payload = card.encode('utf-8')
    scan_secret_payload(card_payload, 'README.md', secrets)
    manifest['files'].append(dict(path='README.md', sha256=hashlib.sha256(card_payload).hexdigest(), rows=0))
    manifest['files'].sort(key=lambda f: f['path'])
    scan_secret_payload(json.dumps(manifest, default=str).encode(), "release.json", secrets)
    for name, payload in shard_payloads.items():
        scan_secret_payload(payload, name, secrets)
    new_paths = {f["path"] for part in parts for f in part["files"]}
    destination = settings.snapshots_dir / job
    def load(name):
        if name == 'README.md':
            return card_payload
        if name in shard_payloads:
            return shard_payloads[name]
        return _load(root if name in new_paths else parent_root, name)
    _safe_path(destination)
    if destination.exists():
        p._verify_local(destination, manifest)
    else:
        materialize(manifest, destination, load, enabled=True)
    from .releases.activate import _atomic_write
    manifest_path = settings.manifests_dir / (job + ".json")
    _safe_path(manifest_path)
    _atomic_write(manifest_path, dumps_manifest(manifest).encode("utf-8"))
    data['snapshot_digest'] = manifest_digest(manifest)
    store._connection.execute("UPDATE collection_jobs SET state='built',data=? WHERE job_id=?", (_dump(data), job))
    return {"state": "validated", "pairs": len(parts) // 2, "bytes": total}
