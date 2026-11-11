# SPDX-License-Identifier: GPL-3.0-only
"""Fail-closed P0 orchestration, independent of the command-line interface.

A release declares both P0 services for one region/month. Each is collected once;
only the second successful normalization advances collecting -> collected ->
normalized. Failed/partial releases are terminal: use a new ID, never overwrite.
Operator attestations live in data_root/operator-policy.json (see RUNBOOK).
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import os
from types import SimpleNamespace
from pathlib import Path
import re
import shutil
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import pyarrow.parquet as pq
from huggingface_hub import HfApi, hf_hub_download

from .adapters import molit_rtms
from .credentials import load_secret, redact
from .models import ObservationStatus
from .normalization.rtms import normalize
from .registry import ServiceRegistry
from .releases.activate import activate as replace_active, rollback as restore_previous
from .releases.hf import _HubWriter, download_bytes
from .releases import workspace as workspace_guard
from .releases.manifest import build_manifest, dump_manifest, load_manifest as _decode_manifest, relative_path, manifest_digest
from .storage.parquet import SCHEMA, schema_for, _safe_path, read_partition, write_partition
from .storage.metadata import read_json, read_metadata
from .storage.query import TransactionQuery
from .storage.state import StateStore

SERVICES = ("molit-apartment-sale", "molit-apartment-rent")


class PipelineError(ValueError):
    """Fixed, value-free operational error."""

    def __init__(self, code):
        super().__init__(code)
        self.error_code = code


def _require(condition, code):
    if not condition:
        raise PipelineError(code)


def _json_write(path, value):
    _safe_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str) + "\n"
    temporary = path.with_suffix(path.suffix + ".tmp")
    _safe_path(temporary)
    temporary.write_text(payload, encoding="utf-8", newline="\n")
    temporary.replace(path)


def _id(value):
    _require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", value), "invalid_release_id")
    return value


def _root(settings, release_id):
    _check_identifiers(_loaded_secrets(settings), _id(release_id))
    root = settings.staging_dir / release_id
    _safe_path(root)
    return root


def _metadata(settings, release_id):
    return _root(settings, release_id) / "collection.json"


def _policy(settings):
    path = settings.data_root / "operator-policy.json"
    _safe_path(path)
    try:
        policy = read_json(path)
        _require(isinstance(policy, dict), "operator_policy_required")
        return policy
    except (OSError, ValueError):
        raise PipelineError("operator_policy_required") from None


def _state(store, release_id, expected):
    row = store.get_release(release_id)
    _require(row is not None and row["state"] == expected, "unexpected_release_state")


def _manifest_path(settings, release_id):
    _check_identifiers(_loaded_secrets(settings), _id(release_id))
    path = settings.manifests_dir / (release_id + ".json")
    _safe_path(path)
    return path


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _secret_aliases(settings, *, hf=False, read_only=False):
    # Keep diagnostics and publication aligned, including unused credentials
    # that must still be loaded for secret scanning.
    from .credentials.loader import ENV_NAMES
    aliases = {"data_go_kr"} | set(settings.credential_files)
    aliases.update(alias for alias, name in ENV_NAMES.items() if name in os.environ)
    if hf:
        aliases.add("hugging_face_read")
        if not read_only:
            aliases.add("hugging_face_write")
    return sorted(aliases)


def _loaded_secrets(settings, *, hf=False, read_only=False):
    # Never silently fall back to an empty scan set. The two P0 credentials are
    # mandatory for publishing; scan any other explicitly configured aliases too.
    secrets = [load_secret(alias, settings)
               for alias in _secret_aliases(settings, hf=hf, read_only=read_only)]
    from .releases.hf import scan_secret_payload
    for name in (settings.hf_namespace, *settings.hf_repos.values(), *settings.hf_staging_names.values()):
        if name is not None:
            scan_secret_payload(name.encode("utf-8"), "repository-identifier", secrets)
    return secrets


def _check_identifiers(secrets, *values):
    from .releases.hf import scan_secret_payload
    for value in values:
        scan_secret_payload(str(value).encode("utf-8"), "identifier", secrets)


def _secret_check(path, secrets):
    from .releases.hf import scan_secret_payload
    scan_secret_payload(path.read_bytes(), path.name, secrets)


def _verify_complete(root, meta, *, data_schema_version="kapa-runtime-data/2"):
    """Verify declared services and replay normalization, not just row counts."""
    _require(set(meta["completed"]) == set(meta["services"]) == set(SERVICES), "incomplete_services")
    files = [f for c in meta["completed"].values() for f in c["files"]]
    _require(all(c["files"] for c in meta["completed"].values()), "missing_normalized_files")
    _verify_local(root, {"files": files, "data_schema_version": data_schema_version})
    from .credentials import Secret
    for service, completed in meta["completed"].items():
        pages, totals, sizes = [], [], []
        _id(completed["observation_id"])
        raw_root = root / "raw" / service / completed["observation_id"]
        _safe_path(raw_root)
        for path in sorted(raw_root.glob("page-*.xml")):
            _safe_path(path)
            page, total, size = molit_rtms._parse(path.read_bytes(), Secret("TEST-replay-redaction-sentinel"))
            pages.append(page)
            totals.append(total)
            sizes.append(size)
        _require(bool(pages) and len(set(totals)) == len(set(sizes)) == 1
                 and sum(len(page.items) for page in pages) == totals[0]
                 and (completed["status"] == "zero") == (totals[0] == 0),
                 "invalid_collection_evidence")
        _require(len(pages) == completed["pages_expected"] == completed["pages_received"]
                 and [page.page_no for page in pages] == list(range(1, len(pages) + 1))
                 and completed["status"] in ("success", "zero"), "invalid_collection_evidence")
        obs = SimpleNamespace(service_id=service, observation_id=completed["observation_id"],
                              request={"LAWD_CD": meta["lawd_code"]},
                              fetched_at=datetime.fromisoformat(completed["fetched_at"]))
        expected = normalize(SimpleNamespace(observation=obs, pages=pages),
                             transaction_type=service.rsplit("-", 1)[1], data_schema_version=data_schema_version)
        actual = [r for f in completed["files"] for r in read_partition(root / f["path"], schema_version=data_schema_version)]
        _require(len(expected) == len(actual) == completed["rows"], "normalization_count_mismatch")
        # Identical provider rows share a version ID but retain distinct page/item
        # references. Parquet's tie-break order need not match source order (e.g.
        # item 10 sorts before item 2). Pair by durable provenance as well, while
        # still comparing every field and retaining duplicate multiplicity.
        replay_key = lambda r: (r.record_version_id, r.source_payload_ref)
        _require(sorted(expected, key=replay_key) ==
                 sorted(actual, key=replay_key), "normalization_replay_mismatch")
    return files


def _finish_collection(store, release_id, root, meta, secrets):
    """Recover only from durable, complete, hash-verified normalization output."""
    try:
        files = _verify_complete(root, meta)
        completed = list(meta["completed"].values())
        for f in files:
            _secret_check(root / f["path"], secrets)
        checked = [r for f in files for r in read_partition(root / f["path"])]
        valid = len(checked) == sum(c["rows"] for c in completed)
        _require(valid, "normalization_count_mismatch")
        guards = {
            "all_pages_received": all(c["pages_expected"] == c["pages_received"] for c in completed),
            "responses_valid": all(c["status"] in ("success", "zero") for c in completed),
            "zero_envelope_valid": all(c["status"] != "zero" or c["rows"] == 0 and c["pages_received"] == 1 for c in completed)}
        _require(all(guards.values()), "invalid_collection_evidence")
    except Exception:
        store.transition_release(release_id, "failed", {"reason": "recovery_output_verification_failed"})
        raise PipelineError("recovery_output_verification_failed") from None
    if store.get_release(release_id)["state"] == "collecting":
        store.transition_release(release_id, "collected", guards)
    store.transition_release(release_id, "normalized", {name: valid for name in
        ("units_valid", "dates_valid", "fields_valid", "corrections_valid")})


def collect(settings, *, service, lawd_code, deal_ymd, release_id=None, client=None,
            clock=lambda: datetime.now(ZoneInfo("Asia/Seoul"))):
    _require(service in SERVICES, "unsupported_service")
    _require(isinstance(lawd_code, str) and re.fullmatch(r"[0-9]{5}", lawd_code), "invalid_scope")
    _require(isinstance(deal_ymd, str) and re.fullmatch(r"[0-9]{6}", deal_ymd), "invalid_scope")
    try:
        datetime.strptime(deal_ymd, "%Y%m")
    except ValueError:
        raise PipelineError("invalid_scope") from None
    release_id = _id(release_id or ("r-" + uuid4().hex))
    root = _root(settings, release_id)
    spec = ServiceRegistry.load().get(service)
    secret = load_secret(spec.credential_alias, settings)
    secrets = _loaded_secrets(settings)
    _check_identifiers(secrets, release_id, service, lawd_code, deal_ymd, spec.provider, spec.dataset_id)
    policy = _policy(settings).get("services", {}).get(service, {})
    budget = policy.get("max_requests")
    quota_ok = type(budget) is int and 3 <= budget <= 150
    guards = {
        "approval_verified": spec.operation_verified is True and spec.approval_status.startswith("connection-verified-"),
        "scope_verified": spec.host == "apis.data.go.kr" and service in SERVICES,
        "quota_verified": quota_ok,
        "max_requests": budget,
        "max_pages": budget // 3 if quota_ok else 0,
        "output_verified": not root.exists() or root.is_dir(),
    }
    _require(all(guards.values()), "collection_policy_not_verified")
    run_id = uuid4().hex
    _check_identifiers(secrets, run_id)
    with StateStore(settings.state_db) as store:
        quota_day = clock().astimezone(ZoneInfo("Asia/Seoul")).date().isoformat()
        daily_limit = min(policy.get("daily_limit", 10000), spec.quota["daily_limit"])
        _require(type(daily_limit) is int and daily_limit > 0, "invalid_daily_quota")
        existing = store.get_release(release_id)
        scope = {"release_id": release_id, "services": list(SERVICES),
                 "lawd_code": lawd_code, "deal_ymd": deal_ymd}
        metadata = _metadata(settings, release_id)
        _safe_path(metadata)  # Before any read, including legacy orphan adoption.
        if existing is None:
            # Legacy orphan directories may be adopted only with matching metadata.
            if root.exists():
                try:
                    orphan = read_json(metadata)
                except Exception:
                    raise PipelineError("recovery_metadata_invalid") from None
                _require(isinstance(orphan, dict) and all(orphan.get(k) == v for k, v in scope.items()),
                         "release_scope_mismatch")
                _require(orphan.get("completed") == {} and
                         {child.name for child in root.iterdir()} == {"collection.json"},
                         "orphan_collection_artifacts_not_adoptable")
            store.transition_release(release_id, "draft", scope)
            existing = store.get_release(release_id)
        _require(existing["state"] in ("draft", "collecting", "collected"), "unexpected_release_state")
        if existing["state"] == "draft":
            recorded = existing["guard_evidence"][0]["evidence"]
            _require(all(recorded.get(k) == v for k, v in scope.items()), "release_scope_mismatch")
            if not metadata.exists():
                # Recover either pre-mkdir or post-mkdir/pre-metadata process exit.
                _require(not root.exists() or not any(root.iterdir()), "recovery_metadata_invalid")
                root.mkdir(parents=True, exist_ok=True)
                _json_write(metadata, {**scope, "completed": {}})
        try:
            meta = read_json(metadata)
        except Exception:
            raise PipelineError("recovery_metadata_invalid") from None
        _require(all(meta.get(k) == v for k, v in scope.items()), "release_scope_mismatch")
        current = store.get_release(release_id)["state"]
        if current == "draft":
            store.transition_release(release_id, "collecting", guards)
        if current == "collected" or set(meta["completed"]) == set(SERVICES):
            _finish_collection(store, release_id, root, meta, secrets)
            return {"release_id": release_id, "state": "normalized", "recovered": True}
        if service in meta["completed"]:
            # A crash may leave a fully written service while its sibling is
            # pending. Permit idempotent continuation, but never skip integrity.
            try:
                _verify_local(root, {"files": [f for c in meta["completed"].values() for f in c["files"]]})
            except Exception:
                store.transition_release(release_id, "failed", {"reason": "recovery_output_verification_failed"})
                raise PipelineError("recovery_output_verification_failed") from None
            return {"release_id": release_id, "state": "collecting", "recovered": True}
        _require(store.reserved_requests(service, quota_day) + budget <= daily_limit, "daily_quota_exceeded")
        store.record_run(run_id, service_id=service, lawd_code=lawd_code, deal_ymd=deal_ymd,
                         quota_day=quota_day, reserved_requests=budget, daily_limit=daily_limit,
                         quota_source=spec.quota["quota_source"])
        reserved_days = {quota_day}
        def reserve_request_day():
            day = clock().astimezone(ZoneInfo("Asia/Seoul")).date().isoformat()
            if day not in reserved_days:
                _require(store.reserved_requests(service, day) + budget <= daily_limit,
                         "daily_quota_exceeded")
                # Conservatively reserve the entire bounded run again on each
                # new execution day, before its first actual page/retry request.
                store.record_run(uuid4().hex, service_id=service, quota_day=day,
                                 reserved_requests=budget, daily_limit=daily_limit,
                                 parent_run_id=run_id, quota_source=spec.quota["quota_source"])
                reserved_days.add(day)
        try:
            def perform(http):
                return molit_rtms.collect(spec, secret, lawd_code=lawd_code, deal_ymd=deal_ymd,
                                          client=http, run_id=run_id, max_pages=budget // 3,
                                          before_request=reserve_request_day)
            if client is None:
                with httpx.Client(timeout=httpx.Timeout(30, connect=10), follow_redirects=False,
                                  trust_env=False) as http:
                    result = perform(http)
            else:
                result = perform(client)
            observation = result.observation
            _check_identifiers(secrets, observation.observation_id, observation.run_id,
                               observation.service_id, observation.dataset_id, observation.endpoint_id)
            store.record_observation(observation)
            raw = root / "raw" / service / observation.observation_id
            raw.mkdir(parents=True)
            for page in result.pages:
                path = raw / f"page-{page.page_no:05d}.xml"
                _require(hashlib.sha256(page.sanitized).hexdigest() == page.sha256, "page_hash_mismatch")
                _require(redact(page.sanitized.decode("utf-8"), [secret]) == page.sanitized.decode("utf-8"), "secret_in_page")
                from .releases.hf import scan_secret_payload
                path.write_bytes(page.sanitized)
            if observation.status in (ObservationStatus.FAILURE, ObservationStatus.PARTIAL):
                branch = "partial" if observation.status == ObservationStatus.PARTIAL else "failed"
                store.transition_release(release_id, branch, {"reason": "collection_" + branch,
                    "limited_scope": observation.coverage})
                return {"release_id": release_id, "state": branch, "observation_id": observation.observation_id}
            normalization_warnings = []
            records = normalize(result, transaction_type=service.rsplit("-", 1)[1], warnings=normalization_warnings)
            if normalization_warnings:
                observation = replace(observation, warnings=observation.warnings + tuple(normalization_warnings))
                store.update_observation_warnings(observation)
            files = write_partition(records, root, asset_type="apartment", transaction_type=service.rsplit("-", 1)[1],
                                    lawd_code=lawd_code, contract_year=int(deal_ymd[:4]), contract_month=int(deal_ymd[4:]))
            for file in files:
                _secret_check(file.path, secrets)
            meta["completed"][service] = {"run_id": run_id, "observation_id": observation.observation_id,
                "fetched_at": observation.fetched_at.isoformat(),
                "rows": len(records), "pages_expected": observation.pages_expected,
                "pages_received": observation.pages_received, "status": str(observation.status),
                "files": [{"path": f.path.relative_to(root).as_posix(), "sha256": f.sha256, "rows": f.rows} for f in files]}
            _json_write(_metadata(settings, release_id), meta)
            if set(meta["completed"]) == set(SERVICES):
                _finish_collection(store, release_id, root, meta, secrets)
            return {"release_id": release_id, "state": store.get_release(release_id)["state"],
                    "observation_id": observation.observation_id, "rows": len(records)}
        except Exception:
            current = store.get_release(release_id)["state"]
            if current in ("collecting", "collected"):
                store.transition_release(release_id, "failed", {"reason": "collection_or_normalization_failed"})
            raise PipelineError("collection_or_normalization_failed") from None


def _git_revision(settings):
    # Keep the lexical checkout until the guard rejects reparse points.
    repo = Path(__file__).absolute().parents[2]
    def git(*args):
        return workspace_guard.git(repo, *args, trusted_repo_owners=settings.trusted_repo_owners)
    try:
        _require(not git("status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=none"), "source_repository_dirty")
        sha = git("rev-parse", "HEAD")
        _require(re.fullmatch(r"[0-9a-f]{40}", sha), "invalid_source_revision")
        return sha
    except workspace_guard.WorkspaceError as exc:
        if str(exc) == "workspace_repository_submodules_unsupported":
            raise PipelineError(str(exc)) from None
        raise PipelineError("source_revision_unavailable") from None


def _verify_workspace(settings, manifest):
    try:
        workspace_guard.verify(settings, manifest)
    except workspace_guard.WorkspaceError:
        raise PipelineError("workspace_revision_mismatch") from None


def _verify_local(root, manifest):
    version = manifest.get("data_schema_version", "kapa-runtime-data/2")
    expected_schema = schema_for(version)
    from .releases.snapshot import inventory
    selected = inventory(manifest, root)
    expected = {f["path"] for f in selected if f["path"].endswith(".parquet")}
    actual = {p.relative_to(root).as_posix() for p in root.rglob("*.parquet")}
    _require(expected == actual, "snapshot_file_set_mismatch")
    for entry in selected:
        path = root / relative_path(entry["path"])
        _safe_path(path)
        _require(_hash(path) == entry["sha256"], "local_hash_mismatch")
        if path.suffix == ".parquet":
            _require(pq.read_schema(path).equals(expected_schema, check_metadata=True), "schema_mismatch")
            _require(len(read_partition(path, schema_version=version)) == entry["rows"], "row_count_mismatch")


def dataset_card(settings, meta, manifest):
    """Render a portable HF card from registry, release and operator evidence."""
    if settings.hf_visibilities.get("market", "private") != "public":
        return ('---\nlanguage: [ko]\npretty_name: "Private KAPA market snapshot"\n'
                'tags: [real-estate]\n---\n# Private KAPA market snapshot\n\n'
                '제한 지역·월의 비공개 관측. 부분 주소이며 취소 거래를 보존한다. '
                '법적 검증 아님 (legal_verified=false).\n')
    registry = ServiceRegistry.load()
    lines = ['---', 'license: other',
             'license_name: data-go-kr-unrestricted',
             'license_link: https://www.data.go.kr/ugs/selectPortalPolicyView.do',
             'language: [ko]', 'pretty_name: "KAPA 아파트 실거래가"',
             'tags: [real-estate, transactions, korea]', '---',
             '# KAPA 아파트 실거래가', '', '## 출처',
             '국토교통부 아파트 매매/전월세 실거래가 자료 (공공데이터포털).',
             '이용허락범위 제한 없음. 아래 서비스별 이용조건 검토 근거를 함께 확인한다.']
    for service in meta['services']:
        spec = registry.get(service)
        dataset = spec.dataset_id
        _require(re.fullmatch(r'[0-9]+', dataset), 'invalid_catalog_id')
        basis = manifest['quality']['terms_evidence'][service]['terms_basis']
        lines.append(f'- {service}: dataset ID {dataset}, '
                     f'https://www.data.go.kr/data/{dataset}/openapi.do')
        lines.append(f'  이용조건 검토 근거: {basis}')
    lines += ['', '## 수집 범위',
              f'- lawd_code: {meta["lawd_code"]}', f'- deal_ymd: {meta["deal_ymd"]}']
    for service in meta['services']:
        completed = meta['completed'][service]
        fetched = completed.get('fetched_at')
        if fetched is None:
            # Compatibility with releases collected before fetched_at was added
            # to the local index: normalized timestamps retain observation time.
            records = [r for f in completed['files'] for r in
                       read_partition(_root(settings, meta['release_id']) / f['path'])]
            fetched = max((r.last_seen_at for r in records), default=None)
        lines.append(f'- {service}: {completed["rows"]}행; fetched_at: {fetched or "미확인"}')
    lines += ['', '## 스키마 요약',
              '- extra: registry-allowlisted fields only; unknown fields remain local; encoding scans are defense in depth.'
              '- 컬럼: ' + ', '.join(SCHEMA.names),
              '- 금액은 제공기관 만원 단위를 KRW 정수로 변환한다. 매매 price_krw, '
              '임대차 deposit_krw/monthly_rent_krw를 분리하며 임의 환산하지 않는다.',
              '- area_m2: 전용면적 m², decimal128(12,4). 날짜와 시간대 있는 수집시각을 구별한다.',
              '- 취소 행도 cancellation_status/cancellation_date 플래그와 함께 보존하며 조회는 기본 제외한다.',
              '', '## 한계',
              '- 부분 주소만 제공하며 동/호는 미공개다. 개별 물건 식별과 매칭은 확정이 아니다.',
              '- 공식 공개시각은 미확인이다. 수집시각은 공개시각을 의미하지 않는다.',
              '- legal_verified=false. 감정평가액 또는 법적 판단이 아니며 관측 범위 밖 완전성을 보장하지 않는다.',
              '', '## 재현성', '- 후보 manifest: manifests/release.json',
              f'- parser_version: {manifest["parser_version"]}',
              f'- data_schema_version: {manifest["data_schema_version"]}',
              f'- release_id: {manifest["release_id"]}',
              '- 파일 해시·행 수·소스 revision은 manifest에 고정한다. raw 봉투와 운영 로그는 공개하지 않는다.', '']
    return '\n'.join(lines)


def build(settings, *, release_id):
    root = _root(settings, release_id)
    with StateStore(settings.state_db) as store:
        row = store.get_release(release_id)
        _require(row is not None and row["state"] == "normalized",
                 "build_requires_normalized_release_run_collect_to_recover")
        meta = read_json(_metadata(settings, release_id))
        sha = _git_revision(settings)
        try:
            siblings, workspace_warnings = workspace_guard.capture(settings)
        except workspace_guard.WorkspaceError as exc:
            raise PipelineError(str(exc)) from None
        policy = _policy(settings)
        terms_evidence = {s: policy.get("services", {}).get(s, {}) for s in SERVICES}
        terms = all(p.get("terms_reviewed") is True and isinstance(p.get("terms_basis"), str)
                    and bool(p["terms_basis"].strip()) for p in terms_evidence.values())
        fixtures = policy.get("fixture_evidence", {})
        fixture_ok = fixtures.get("github_revision") == sha and fixtures.get("passed") is True
        _require(terms and fixture_ok, "build_evidence_required")
        secrets = _loaded_secrets(settings)
        files = sorted(_verify_complete(root, meta), key=lambda f: f["path"])
        m = build_manifest(release_id=release_id, state="validated", files=files,
            parser_version=molit_rtms.PARSER_VERSION, source_runs=[c["run_id"] for c in meta["completed"].values()],
            github_revisions={"pacappkor/kapa_runtime": sha, **siblings},
            coverage={"services": list(SERVICES), "lawd_code": meta["lawd_code"], "deal_ymd": meta["deal_ymd"], "complete": True},
            quality={"passed": True, "legal_verified": False, "fixture_revision": sha,
                     "terms_evidence": terms_evidence, "workspace_warnings": workspace_warnings})
        registry = ServiceRegistry.load()
        m['normalized_extra_fields'] = {
            service: sorted(registry.get(service).normalized_extra_fields) for service in SERVICES}
        _verify_local(root, m)
        for f in files:
            _require(not re.search(r"key|token|secret|\.env", f["path"], re.I), "unsafe_filename")
            _secret_check(root / f["path"], secrets)
        from .releases.hf import scan_secret_payload
        card = dataset_card(settings, meta, m)
        scan_secret_payload(card.encode("utf-8"), "README.md", secrets)
        _require(not re.search(r"(?:(?<![A-Za-z])[A-Za-z]:[\\/]|file://|/Users/|/home/)", card), "local_path_in_card")
        (root / "README.md").write_text(card, encoding="utf-8", newline="\n")
        scan_secret_payload(json.dumps(m["quality"], ensure_ascii=False).encode(), "summary.json", secrets)
        _json_write(root / "quality" / "summary.json", m["quality"])
        for name in ("README.md", "quality/summary.json"):
            path = root / name
            _secret_check(path, secrets)
            m["files"].append({"path": name, "sha256": _hash(path), "rows": 0})
        m["files"].sort(key=lambda f: f["path"])
        from .releases.hf import scan_secret_payload
        scan_secret_payload(json.dumps(m, ensure_ascii=False).encode(), "release.json", secrets)
        manifest_path = _manifest_path(settings, release_id)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        dump_manifest(m, manifest_path)
        (root / "manifests").mkdir(exist_ok=True)
        dump_manifest(m, root / "manifests" / "release.json")
        store.transition_release(release_id, "validated", {
            "rows_verified": sum(f["rows"] for f in files) == sum(c["rows"] for c in meta["completed"].values()),
            "hashes_verified": all(_hash(root / f["path"]) == f["sha256"] for f in m["files"]),
            "privacy_checked": all(r.address_visibility == "partial" for f in files for r in read_partition(root / f["path"])),
            "secrets_checked": bool(secrets), "terms_verified": terms, "fixtures_passed": fixture_ok,
            "terms_evidence_sha256": hashlib.sha256(json.dumps(terms_evidence, sort_keys=True).encode()).hexdigest(),
            "fixture_revision": sha,
            "validated_manifest_sha256": manifest_digest(m),
            "validated_files": m["files"]})
        return {"release_id": release_id, "state": "validated", "files": len(m["files"]),
                "warnings": workspace_warnings}


def _read_manifest(path):
    path = Path(path)
    _safe_path(path)
    return _decode_manifest(path)


class _HubApi:
    """HfApi does not expose the module-level download helper on all versions."""
    def __init__(self, secret):
        self.token = secret.reveal()
        self.api = HfApi(token=self.token)

    def __getattr__(self, name):
        return getattr(self.api, name)

    def hf_hub_download(self, *args, **kwargs):
        return hf_hub_download(*args, **kwargs)


def _publisher(settings, api=None, *, staging=False, restoration=None, read_only=False):
    secrets = _loaded_secrets(settings, hf=True, read_only=read_only)
    _require(bool(secrets), "secret_scan_set_required")
    hf_secret = load_secret("hugging_face_read" if read_only else "hugging_face_write", settings)
    read_secret = load_secret("hugging_face_read", settings)
    name = settings.hf_repos.get("market")
    if staging:
        name = settings.hf_staging_names.get("market", name + "-staging")
    return _HubWriter(api if api is not None else _HubApi(hf_secret), settings.hf_namespace,
                       name, secret_values=lambda: secrets, role="market",
                       terms_evidence=({s: _policy(settings).get("services", {}).get(s, {}) for s in SERVICES}
                                       if restoration is None else None),
                       restoration=restoration, read_token=read_secret.reveal(), write_token=hf_secret.reveal(),
                       expected_visibility="private" if staging else settings.hf_visibilities.get("market", "private"))


def _allowlist(root, manifest):
    names = [f["path"] for f in manifest["files"]] + ["manifests/release.json"]
    _require(all(n.startswith("data/") and n.endswith(".parquet") or n in
                 ("README.md", "quality/summary.json", "manifests/release.json") or n.startswith("schemas/") for n in names), "invalid_allowlist")
    return sorted(names)


def _trusted_published_manifest(settings, store, release_id, *, path=None):
    manifest = _read_manifest(path or _manifest_path(settings, release_id))
    row = store.get_release(release_id)
    evidence = [e["evidence"] for e in row["guard_evidence"] if e["state"] == "published"
                and e["evidence"].get("commit_reread") is True] if row else []
    _require(manifest["release_id"] == release_id and len(evidence) == 1
             and evidence[0].get("published_manifest_sha256") == manifest_digest(manifest),
             "published_manifest_untrusted")
    schema_for(manifest["data_schema_version"])
    return manifest


def _retained_owners(settings, store, repo_id, *, as_of=None, tombstones=None):
    cutoff = None
    if as_of is not None:
        _trusted_published_manifest(settings, store, as_of)
        order = [e['at'] for e in store.get_release(as_of)['guard_evidence']
                 if e['state'] == 'published' and e['evidence'].get('commit_reread') is True]
        _require(len(order) == 1, 'retained_publish_order_required')
        cutoff = order[0]
    active_id = (_read_manifest(settings.active_manifest)["release_id"]
                 if settings.active_manifest.exists() else None)
    if active_id is not None:
        _trusted_published_manifest(settings, store, active_id, path=settings.active_manifest)
    required_id = as_of if as_of is not None else active_id
    published = []
    for row in store.list_releases():
        if row["state"] not in ("published", "active"):
            continue
        order = [e["at"] for e in row["guard_evidence"] if e["state"] == "published"
                 and e["evidence"].get("commit_reread") is True]
        _require(len(order) == 1, "retained_publish_order_required")
        if cutoff is not None and order[0] > cutoff:
            continue
        try:
            manifest = _trusted_published_manifest(settings, store, row["release_id"])
        except PipelineError:
            _require(row["release_id"] != active_id, "active_published_manifest_untrusted")
            continue
        refs = manifest["hugging_face_revisions"] + ([manifest["staging_revision"]] if manifest.get("staging_revision") else [])
        if any(ref["repo_id"] == repo_id for ref in refs):
            preferred = row['release_id'] == required_id
            published.append((preferred, order[0], row["release_id"], manifest))
    owners = {}
    for _, _, release_id, manifest in sorted(published):
        row = store.get_release(release_id)
        removed = set()
        for event in row['guard_evidence']:
            evidence = event['evidence']
            if (evidence.get('reason') != 'retained_partition_removed'
                    or (cutoff is not None and event['at'] > cutoff)):
                continue
            removal_repo = evidence.get('repo_id')
            if removal_repo is None:
                # Old records identify only the owner. A single-repository
                # publication is unambiguous; public/staging publications are not.
                contexts = [e['evidence'] for e in row['guard_evidence']
                            if e['state'] == 'uploaded-candidate' and e['at'] <= event['at']
                            and e['evidence'].get('repo_id')]
                repositories = {ref['repo_id'] for ref in manifest['hugging_face_revisions']}
                if manifest.get('staging_revision'):
                    repositories.add(manifest['staging_revision']['repo_id'])
                _require(len(contexts) == 1 and len(repositories) == 1
                         and contexts[0]['repo_id'] in repositories,
                         'retained_removal_repo_unresolved')
                removal_repo = contexts[0]['repo_id']
            _require(isinstance(removal_repo, str) and bool(removal_repo),
                     'retained_removal_repo_unresolved')
            if removal_repo == repo_id:
                removed.add(evidence['removed_path'])
        from .releases.snapshot import inventory, published_digest
        entries = inventory(manifest, settings.snapshots_dir / release_id)
        for entry in entries:
            if entry['path'].startswith('data/observations/'):
                from .releases.manifest_v2 import resolve_snapshot
                resolved = resolve_snapshot(manifest, lambda name: read_metadata(
                    settings.snapshots_dir / release_id / relative_path(name), as_bytes=True))
                partition = next(p for p in resolved['selected_partitions'] if entry in p['files'])
                producer = partition['producer_release_id']
                if producer != release_id:
                    origin = _trusted_published_manifest(settings, store, producer)
                    _require(partition['origin_digest'] == published_digest(store, producer)
                             and entry in inventory(origin, settings.snapshots_dir / producer),
                             'retained_origin_untrusted')
                else:
                    origin = manifest
                previous = owners.get(entry['path'])
                _require(previous is None or previous[2] == entry, 'immutable_path_conflict')
                if entry['path'] not in removed:
                    owners[entry['path']] = (producer, origin, entry)
                else:
                    owners.pop(entry['path'], None)
                    if tombstones is not None:
                        tombstones.add(entry['path'])
                continue
            # Historical verification requires the target, not today's active owner.
            if entry['path'] not in removed or release_id == required_id:
                owners[entry["path"]] = (release_id, manifest, entry)
                if tombstones is not None:
                    tombstones.discard(entry['path'])
            else:
                # A removed latest owner shadows older publications as well.
                owners.pop(entry['path'], None)
                if tombstones is not None:
                    tombstones.add(entry['path'])
    # Observation withdrawals are permanent, independent of selection order.
    withdrawn = {event['evidence']['removed_path']
                 for row in store.list_releases() for event in row['guard_evidence']
                 if event['evidence'].get('reason') == 'retained_partition_removed'
                 and event['evidence'].get('repo_id') == repo_id
                 and event['evidence'].get('removed_path', '').startswith('data/observations/')
                 and (cutoff is None or event['at'] <= cutoff)}
    for name in withdrawn:
        owners.pop(name, None)
    if tombstones is not None:
        tombstones.update(withdrawn)
    return owners, active_id


def _previous_repo_files(settings, store, repo_id, *, as_of=None):
    owners, _ = _retained_owners(settings, store, repo_id, as_of=as_of)
    return [dict(entry, data_schema_version=manifest["data_schema_version"])
            for _, manifest, entry in owners.values()]


def _promotion_retained_files(settings, store, staging_repo, public_repo):
    retained = {e['path']: e for e in _previous_repo_files(settings, store, staging_repo)}
    tombstones = set()
    public_owners, _ = _retained_owners(settings, store, public_repo, tombstones=tombstones)
    retained.update({name: dict(entry, data_schema_version=manifest['data_schema_version'])
                     for name, (_, manifest, entry) in public_owners.items()})
    # Destination removals must delete staging keys, not merely omit public keys.
    for name in tombstones:
        retained.pop(name, None)
    return list(retained.values())


def _retained_repair(settings, store, repo_id, warnings):
    selected, active_id = _retained_owners(settings, store, repo_id)
    owners = {name: [owner] for name, owner in selected.items()}

    def repair(name, entries):
        from .releases.hf import PublishError, scan_secret_payload
        for release_id, manifest, entry in owners.get(name, []):
            if not any(all(e.get(k) == v for k, v in entry.items()) for e in entries):
                continue
            try:
                snapshot = settings.snapshots_dir / _id(release_id)
                _safe_path(snapshot)
                _verify_local(snapshot, manifest)
                path = snapshot / relative_path(name)
                _safe_path(path)
                payload = path.read_bytes()
                _require(hashlib.sha256(payload).hexdigest() == entry['sha256'], 'repair_digest_mismatch')
                scan_secret_payload(payload, name, _loaded_secrets(settings, hf=True),
                                    schema_version=manifest["data_schema_version"])
                return payload
            except Exception:
                continue
        if name.startswith('data/observations/') and owners.get(name):
            raise PublishError('immutable_retained_partition_unrecoverable')
        if any(release_id == active_id for release_id, _, _ in owners.get(name, [])):
            raise PublishError("active_retained_partition_unrecoverable")
        for release_id, _, _ in owners.get(name, []):
            warning = {"code": "retained_partition_removed", "release_id": release_id, "path": name}
            warnings.append(warning)
            # Conservative durable warning before the remote commit: a failed
            # commit must never leave the ledger claiming complete service.
            store._record_serving_removal(release_id, name, repo_id)
        return None
    return repair


def _restore_public(settings, store, release_id, api):
    pending = store.promotion(release_id)
    if pending is None:
        outstanding = store.pending_promotions()
        if not outstanding:
            return
        release_id = outstanding[0]
        pending = store.promotion(release_id)
    payloads = {name: bytes.fromhex(value) for name, value in pending["restore_payloads"].items()}
    target = settings.hf_namespace + "/" + settings.hf_repos["market"]
    _require(target == pending["repo_id"], "promotion_target_changed")
    expected = pending.get("public_commit_sha")
    _require(isinstance(expected, str) and re.fullmatch(r"[0-9a-fA-F]{40}", expected),
             "public_recovery_expected_head_required")
    authority = store._restoration_authority(release_id, target)
    publisher = _publisher(settings, api, restoration=authority)
    sha = publisher._restore_journal()
    pending["public_commit_sha"] = sha
    store.save_promotion(release_id, pending)
    publisher._verify_inventory(sha, payloads, settings.cache_dir / "restore-result")
    publisher._require_head(sha)
    row = store.get_release(release_id)
    if row["state"] not in ("quarantined", "failed"):
        store.transition_release(release_id, "quarantined", {"reason": "public_promotion_restored",
                                 "restore_commit_sha": sha, "repo_id": publisher.repo_id})
    store.finish_promotion(release_id)
    raise PipelineError("public_promotion_restored_quarantined")


def _public_restore_inventory(settings, store, publisher):
    payloads = {}
    if settings.active_manifest.exists():
        pointer = _read_manifest(settings.active_manifest)
        old = _trusted_published_manifest(settings, store, pointer["release_id"], path=settings.active_manifest)
        ref = next((r for r in old["hugging_face_revisions"] if r["repo_id"] == publisher.repo_id), None)
        _require(ref is not None, "public_restore_target_missing")
        # Current-branch verification below checks all owners together. Older
        # pinned active commits cannot contain later published partitions.
    # Preserve only scanned, baseline-bound attributes from the current branch.
    revision, remote = publisher._head_tree()
    trusted = []
    for row in store.list_releases():
        if row['state'] not in ('published', 'active'):
            continue
        try:
            current = _trusted_published_manifest(settings, store, row['release_id'])
        except PipelineError:
            continue
        if any(ref['repo_id'] == publisher.repo_id for ref in current['hugging_face_revisions']):
            trusted.append(current)
    if not settings.active_manifest.exists() and not trusted:
        _require(set(remote) <= {".gitattributes"}, "public_repo_not_empty_no_restore_target")
    else:
        report = None
        for current in trusted:
            current['hugging_face_revisions'] = [{'repo_id': publisher.repo_id, 'commit_sha': revision}]
            candidate = publisher.verify(revision, current, settings.cache_dir / 'restore-current',
                previous_files=_previous_repo_files(settings, store, publisher.repo_id), evidence_store=store)
            if candidate.passed:
                report = candidate
                break
        _require(report is not None, "public_restore_inventory_unverified")
        for name, digest, _ in report.verified_inventory:
            payload = download_bytes(publisher.api, publisher.repo_id, name, revision=revision,
                cache_dir=settings.cache_dir / "restore-frozen", token=publisher._read_token)
            _require(hashlib.sha256(payload).hexdigest() == digest, "public_restore_bytes_changed")
            payloads[name] = payload
    if ".gitattributes" in remote:
        payload = download_bytes(publisher.api, publisher.repo_id, ".gitattributes",
                         revision=revision, cache_dir=settings.cache_dir / "restore-attributes",
                         token=publisher._read_token)
        from .releases.hf import scan_secret_payload
        scan_secret_payload(payload, ".gitattributes", publisher.secret_values())
        store.bind_repository_attributes(publisher.repo_id, hashlib.sha256(payload).hexdigest())
        payloads[".gitattributes"] = payload
    else:
        store.bind_repository_attributes(publisher.repo_id, None)
    return payloads, revision


def publish(settings, *, release_id, dry_run=False, api=None):
    root = _root(settings, release_id)
    with StateStore(settings.state_db) as store:
        _restore_public(settings, store, release_id, api)
        existing = store.get_release(release_id)
        if existing is not None and existing["state"] == "uploaded-candidate":
            uploaded = existing["guard_evidence"][-1]["evidence"]
            store.transition_release(release_id, "quarantined", {"reason": "interrupted_post_upload",
                "repo_id": uploaded["repo_id"], "commit_sha": uploaded["commit_sha"]})
            raise PipelineError("interrupted_post_upload_quarantined")
        _state(store, release_id, "validated")
        m = _read_manifest(_manifest_path(settings, release_id))
        _require(m['schema_version'] == 'kapa-runtime-release/1', 'cumulative_publication_not_integrated')
        built = next(e["evidence"] for e in existing["guard_evidence"] if e["state"] == "validated")
        try:
            _require(manifest_digest(m) == built.get("validated_manifest_sha256")
                     and m["files"] == built.get("validated_files")
                     and manifest_digest(_read_manifest(root / "manifests/release.json")) == manifest_digest(m),
                     "validated_manifest_changed_rebuild_required")
            _verify_local(root, m)
        except Exception:
            raise PipelineError("validated_manifest_changed_rebuild_required") from None
        allowlist = _allowlist(root, m)
        candidate_manifest_hash = _hash(root / "manifests/release.json")
        _require(settings.hf_namespace and settings.hf_repos.get("market"), "hf_target_required")
        visibility = settings.hf_visibilities.get("market", "private")
        policy = _policy(settings)
        terms_evidence = {s: policy.get("services", {}).get(s, {}) for s in SERVICES}
        current_digest = hashlib.sha256(json.dumps(terms_evidence, sort_keys=True).encode()).hexdigest()
        built = next(e["evidence"] for e in existing["guard_evidence"] if e["state"] == "validated")
        _require(current_digest == built.get("terms_evidence_sha256"), "terms_changed_rebuild_required")
        if visibility == "public":
            _require(all(policy.get("services", {}).get(s, {}).get("terms_reviewed") is True
                         and bool(policy["services"][s].get("terms_basis", "").strip())
                         for s in m["coverage"]["services"]), "public_terms_review_required")
        if dry_run:
            return {"release_id": release_id, "dry_run": True, "visibility": visibility, "repo_id": settings.hf_namespace + "/" + settings.hf_repos["market"], "allowlist": allowlist}
        publisher = _publisher(settings, api)
        publisher.ensure_private_repo()
        visibility_checked = publisher.api.repo_info(publisher.repo_id, repo_type="dataset", token=publisher._read_token).private is publisher.expected_private
        _require(visibility_checked, "repository_visibility_mismatch")
        staging_ref = None
        warnings = []
        if visibility == "public":
            staging = _publisher(settings, api, staging=True)
            staging.ensure_private_repo()
            restore, restore_parent = _public_restore_inventory(settings, store, publisher)
            # Public may retain partitions already lost (or tombstoned) in staging.
            # Bring those verified bytes through private verification again rather
            # than deleting them during exact-inventory promotion.
            retained = _promotion_retained_files(settings, store, staging.repo_id, publisher.repo_id)
            staging_repair = _retained_repair(settings, store, staging.repo_id, warnings)
            def repair_staging(name, entries):
                # Frozen public bytes already passed owner hash/schema/row checks.
                # Prefer them before local repair can reject an active owner.
                payload = restore.get(name)
                if payload is not None and any(e['sha256'] == hashlib.sha256(payload).hexdigest() for e in entries):
                    return payload
                return staging_repair(name, entries)
            staging_sha = staging.upload_candidate(root, allowlist, exact_inventory=True,
                previous_files=retained,
                repair_retained=repair_staging,
                evidence_store=store, cache_dir=settings.cache_dir / "staging-attributes")
            staged = json.loads(json.dumps(m))
            staged["files"].append({"path": "manifests/release.json", "sha256": candidate_manifest_hash, "rows": 0})
            staged["files"].sort(key=lambda f: f["path"])
            staged["state"] = "published"
            staged["hugging_face_revisions"] = [{"repo_id": staging.repo_id, "commit_sha": staging_sha}]
            retained = _promotion_retained_files(settings, store, staging.repo_id, publisher.repo_id)
            report = staging.verify(staging_sha, staged, settings.cache_dir / (release_id + "-private"),
                                    previous_files=retained, evidence_store=store)
            if not report.passed:
                store.transition_release(release_id, "quarantined", {"reason": "private_staging_verification_failed",
                    "repo_id": staging.repo_id, "staging_commit_sha": staging_sha})
                raise PipelineError("private_staging_verification_failed")
            # Freeze the exact private-verified bytes, never reopen mutable staging
            # during promotion. Downloads are checked against the bound inventory.
            payloads = {}
            for name, digest, _ in report.verified_inventory:
                payload = download_bytes(staging.api, staging.repo_id, name,
                    revision=staging_sha, cache_dir=settings.cache_dir / (release_id + "-promote"),
                    token=staging._read_token)
                _require(hashlib.sha256(payload).hexdigest() == digest, "staging_bytes_changed")
                payloads[name] = payload
            restore, restore_parent = _public_restore_inventory(settings, store, publisher)
            store.save_promotion(release_id, {"repo_id": publisher.repo_id, "staging_commit_sha": staging_sha,
                "restore_payloads": {name: value.hex() for name, value in restore.items()}})
            staging_ref = {"repo_id": staging.repo_id, "commit_sha": staging_sha}
            try:
                sha = publisher._promote_verified(staging, report, staged, payloads, evidence_store=store,
                    cache_dir=settings.cache_dir / "public-attributes", expected_parent=restore_parent)
                store.save_promotion(release_id, {"repo_id": publisher.repo_id, "staging_commit_sha": staging_sha,
                    "public_commit_sha": sha, "restore_payloads": {name: value.hex() for name, value in restore.items()}})
            except Exception:
                _restore_public(settings, store, release_id, api)
                raise
        else:
            sha = publisher.upload_candidate(root, allowlist, exact_inventory=True,
                repair_retained=_retained_repair(settings, store, publisher.repo_id, warnings),
                previous_files=_previous_repo_files(settings, store, publisher.repo_id),
                evidence_store=store, cache_dir=settings.cache_dir / "private-inventory")
        try:
            store.transition_release(release_id, "uploaded-candidate", {
                # Legacy guard name means the configured visibility policy was
                # checked; public is only permitted for explicit market releases.
                "private_verified": visibility_checked,
                "expected_visibility": visibility,
                "staging_revision": staging_ref,
                "repo_id": publisher.repo_id, "commit_sha": sha,
                "namespace_verified": publisher.repo_id == settings.hf_namespace + "/" + settings.hf_repos["market"],
                "allowlist_verified": allowlist == _allowlist(root, m)})
        except Exception:
            if store.promotion(release_id) is not None:
                _restore_public(settings, store, release_id, api)
            raise
        try:
            # The uploaded manifest cannot include its own hash or commit SHA.
            # Bind its candidate bytes into the final local published manifest.
            m["files"].append({"path": "manifests/release.json",
                               "sha256": candidate_manifest_hash, "rows": 0})
            m["files"].sort(key=lambda f: f["path"])
            m["hugging_face_revisions"] = [{"repo_id": publisher.repo_id, "commit_sha": sha}]
            m["state"] = "published"
            if staging_ref is not None:
                m["staging_revision"] = staging_ref
            report = publisher.verify(sha, m, settings.cache_dir / release_id,
                                      previous_files=_previous_repo_files(settings, store, publisher.repo_id), evidence_store=store)
            if not report.passed:
                raise PipelineError("remote_verification_failed")
            publisher._require_head(sha)
            snapshot = settings.snapshots_dir / release_id
            _safe_path(snapshot)
            _require(not snapshot.exists(), "snapshot_already_exists")
            snapshot.mkdir(parents=True)
            for f in m["files"]:
                dest = snapshot / f["path"]
                dest.parent.mkdir(parents=True, exist_ok=True)
                if staging_ref is not None:
                    dest.write_bytes(payloads[f["path"]])
                else:
                    shutil.copyfile(root / f["path"], dest)
            _verify_local(snapshot, m)
            dump_manifest(m, _manifest_path(settings, release_id))
            publisher._require_head(sha)
            store.transition_release(release_id, "published", {"published_manifest_sha256": manifest_digest(m), "commit_reread": report.passed,
                "files_verified": set(report.checked_files) == {f["path"] for f in m["files"]},
                "hashes_verified": not report.errors, "schema_verified": all(pq.read_schema(snapshot / f["path"]).equals(SCHEMA, check_metadata=True) for f in m["files"] if f["path"].endswith(".parquet"))})
            store.finish_promotion(release_id)
            return {"release_id": release_id, "state": "published", "repo_id": publisher.repo_id, "commit_sha": sha,
                    "warnings": warnings}
        except Exception:
            if store.promotion(release_id) is not None:
                _restore_public(settings, store, release_id, api)
            store.transition_release(release_id, "quarantined", {"reason": "post_upload_failed",
                "repo_id": publisher.repo_id, "commit_sha": sha})
            raise PipelineError("post_upload_failed") from None


def attest_legacy(settings, *, release_id, api=None):
    """Explicit read-only remote attestation; never infer trust from local bytes."""
    from .releases.manifest import loads_manifest
    from .releases.hf import scan_secret_payload
    _id(release_id)
    with StateStore(settings.state_db) as store:
        row = store.get_release(release_id)
        _require(row is not None and row["state"] in ("published", "active"), "legacy_release_not_published")
        refs = {(e["evidence"].get("repo_id"), e["evidence"].get("commit_sha"))
                for e in row["guard_evidence"] if e["state"] in ("uploaded-candidate", "published")
                and e["evidence"].get("repo_id") and e["evidence"].get("commit_sha")}
        _require(len(refs) == 1, "legacy_ledger_revision_required")
        repo_id, sha = refs.pop()
        _require(isinstance(sha, str) and re.fullmatch(r"[0-9a-fA-F]{40}", sha), "legacy_ledger_revision_required")
        publisher = _publisher(settings, api, read_only=True)
        _require(repo_id == publisher.repo_id, "legacy_target_mismatch")
        publisher._private()
        local = _read_manifest(_manifest_path(settings, release_id))
        schema_for(local["data_schema_version"])
        _require(local["release_id"] == release_id and local["hugging_face_revisions"] ==
                 [{"repo_id": repo_id, "commit_sha": sha}], "legacy_manifest_revision_mismatch")
        def download(name):
            publisher._scan_path(name)
            return download_bytes(publisher.api, repo_id, name, revision=sha,
                cache_dir=settings.cache_dir / (release_id + "-attestation"), token=publisher._read_token)
        remote_payload = download("manifests/release.json")
        scan_secret_payload(remote_payload, "manifests/release.json", publisher.secret_values())
        remote = loads_manifest(remote_payload.decode("utf-8"))
        # Uploaded candidate predates its own hash and Hub SHA. Derive the final
        # published manifest solely through the original deterministic changes.
        derived = json.loads(json.dumps(remote))
        derived.pop("staging_revision", None)
        derived["state"] = "published"
        derived["hugging_face_revisions"] = [{"repo_id": repo_id, "commit_sha": sha}]
        _require(not any(f["path"] == "manifests/release.json" for f in derived["files"]),
                 "legacy_remote_manifest_self_reference")
        derived["files"].append({"path": "manifests/release.json",
            "sha256": hashlib.sha256(remote_payload).hexdigest(), "rows": 0})
        derived["files"].sort(key=lambda entry: entry["path"])
        # Staging provenance must also come from ledger evidence, never local.
        staging = [e["evidence"].get("staging_revision") for e in row["guard_evidence"]
                   if e["state"] == "uploaded-candidate" and e["evidence"].get("staging_revision")]
        if staging:
            _require(len(staging) == 1, "legacy_staging_evidence_invalid")
            derived["staging_revision"] = staging[0]
        _require(manifest_digest(derived) == manifest_digest(local), "legacy_remote_manifest_mismatch")
        _allowlist(None, local)
        for entry in local["files"]:
            payload = remote_payload if entry["path"] == "manifests/release.json" else download(entry["path"])
            _require(hashlib.sha256(payload).hexdigest() == entry["sha256"], "legacy_remote_hash_mismatch")
            scan_secret_payload(payload, entry["path"], publisher.secret_values(),
                                schema_version=local["data_schema_version"])
            if entry["path"].endswith(".parquet"):
                import pyarrow as pa
                _require(pq.read_metadata(pa.BufferReader(payload)).num_rows == entry["rows"], "legacy_remote_rows_mismatch")
        publisher._private()
        store._attest_legacy(release_id, manifest_digest(local), sha)
        return {"release_id": release_id, "state": row["state"], "attested": True, "commit_sha": sha}


def _require_target_served_at_head(settings, store, publisher, manifest):
    """An old pinned snapshot is selectable only while its bytes are served."""
    try:
        from .releases.hf import scan_secret_payload
        import pyarrow as pa
        head, remote = publisher._head_tree()
        from .releases.snapshot import inventory
        entries = inventory(manifest, settings.snapshots_dir / manifest['release_id'])
        if manifest['schema_version'] == 'kapa-runtime-release/2':
            tombstones = set()
            _retained_owners(settings, store, publisher.repo_id, tombstones=tombstones)
            _require(not tombstones.intersection(e['path'] for e in entries), 'tombstone_resurrection')
        for entry in entries:
            name = entry['path']
            if not (name.startswith('data/') and name.endswith('.parquet')):
                continue
            _require(name in remote, 'active_target_not_served_at_head')
            payload = download_bytes(publisher.api, publisher.repo_id, name, revision=head,
                cache_dir=settings.cache_dir / 'active-target-head', token=publisher._read_token)
            _require(hashlib.sha256(payload).hexdigest() == entry['sha256'], 'active_target_not_served_at_head')
            scan_secret_payload(payload, name, publisher.secret_values(), schema_version=manifest['data_schema_version'])
            _require(pq.read_metadata(pa.BufferReader(payload)).num_rows == entry['rows'], 'active_target_not_served_at_head')
        publisher._require_head(head)
    except Exception:
        raise PipelineError('active_target_not_served_at_head') from None


def _prepare_cumulative(settings, store, manifest, api, enabled):
    if manifest['schema_version'] != 'kapa-runtime-release/2':
        return
    _require(enabled is True, 'cumulative_snapshots_disabled')
    from .releases.snapshot import inventory, materialize
    root = settings.snapshots_dir / manifest['release_id']
    publisher = _publisher(settings, api, read_only=True)
    ref = next(r for r in manifest['hugging_face_revisions'] if r['repo_id'] == publisher.repo_id)
    def load(name):
        return download_bytes(publisher.api, publisher.repo_id, name, revision=ref['commit_sha'],
            cache_dir=settings.cache_dir / manifest['release_id'], token=publisher._read_token)
    if not root.exists():
        materialize(manifest, root, load, enabled=True)
    paths = {e['path'] for e in inventory(manifest, root)}
    removed = {e['evidence'].get('removed_path') for row in store.list_releases()
               for e in row['guard_evidence']
               if e['evidence'].get('reason') == 'retained_partition_removed'
               and e['evidence'].get('repo_id') == publisher.repo_id}
    _require(not paths.intersection(removed), 'tombstone_resurrection')


def activate(settings, *, release_id, api=None, cumulative=False):
    with StateStore(settings.state_db) as store:
        _restore_public(settings, store, release_id, api)
        row = store.get_release(release_id)
        _require(row is not None and row['state'] in ('published', 'active'), 'activation_state_mismatch')
        path = _manifest_path(settings, release_id)
        m = _trusted_published_manifest(settings, store, release_id)
        _verify_workspace(settings, m)
        _prepare_cumulative(settings, store, m, api, cumulative)
        if settings.active_manifest.exists():
            pointer = _read_manifest(settings.active_manifest)
            _trusted_published_manifest(settings, store, pointer["release_id"], path=settings.active_manifest)
        _verify_local(settings.snapshots_dir / release_id, m)
        publisher = _publisher(settings, api, read_only=True)
        ref = next(r for r in m["hugging_face_revisions"] if r["repo_id"] == publisher.repo_id)
        report = publisher.verify(ref["commit_sha"], m, settings.cache_dir / release_id,
                                  previous_files=_previous_repo_files(settings, store, publisher.repo_id,
                                                                      as_of=release_id), evidence_store=store)
        _require_target_served_at_head(settings, store, publisher, m)
        _verify_workspace(settings, m)
        replace_active(path, settings.active_manifest, report,
                       snapshot_root=settings.snapshots_dir / release_id)
        if row['state'] == 'published':
            store.transition_release(release_id, "active", {
                "repo_revisions_verified": report.passed and bool(m["github_revisions"]),
                "manifest_atomically_replaced": read_metadata(settings.active_manifest, as_bytes=True) == read_metadata(path, as_bytes=True)})
        return {"release_id": release_id, "state": "active"}


def rollback(settings, *, api=None, cumulative=False):
    with StateStore(settings.state_db) as store:
        _restore_public(settings, store, "", api)
        previous_path = settings.active_manifest.with_name("previous.json")
        pointer = _read_manifest(previous_path)
        previous = _trusted_published_manifest(settings, store, pointer["release_id"], path=previous_path)
        _id(previous["release_id"])
        _verify_workspace(settings, previous)
        _prepare_cumulative(settings, store, previous, api, cumulative)
        _verify_local(settings.snapshots_dir / previous["release_id"], previous)
        row = store.get_release(previous["release_id"])
        _require(row is not None and row["state"] in ("published", "active"), "rollback_state_mismatch")
        publisher = _publisher(settings, api, read_only=True)
        ref = next((r for r in previous["hugging_face_revisions"] if r["repo_id"] == publisher.repo_id), None)
        _require(ref is not None, "rollback_revision_missing")
        report = publisher.verify(ref["commit_sha"], previous, settings.cache_dir / previous["release_id"],
                                  previous_files=_previous_repo_files(settings, store, publisher.repo_id,
                                                                      as_of=previous['release_id']), evidence_store=store)
        _require(report.passed, "rollback_remote_verification_failed")
        _require_target_served_at_head(settings, store, publisher, previous)
        _verify_workspace(settings, previous)
        restore_previous(settings.active_manifest, report,
                         snapshot_root=settings.snapshots_dir / previous['release_id'])
        if row["state"] == "published":
            store.transition_release(previous["release_id"], "active", {
                "repo_revisions_verified": report.passed,
                "manifest_atomically_replaced": _read_manifest(settings.active_manifest) == previous})
        # An already-active ledger entry stays active: the ledger describes past
        # activations, while active.json alone selects the currently served one.
        return {"release_id": previous["release_id"], "state": "active"}


def query(settings, *, filters, limit=100, release_id=None, cursor=None):
    data_schema_version = "kapa-runtime-data/2"
    if release_id is not None:
        root = _root(settings, release_id)
        with StateStore(settings.state_db) as store:
            row = store.get_release(release_id)
            trusted = (_trusted_published_manifest(settings, store, release_id)
                       if row and row["state"] in ("published", "active") else None)
        _require(row is not None and row["state"] in
                 ("normalized", "validated", "uploaded-candidate", "published", "active"),
                 "query_release_state_not_ready")
        release_state = row["state"]
        try:
            meta = ({"services": trusted["coverage"]["services"]} if trusted is not None
                    else read_json(_metadata(settings, release_id)))
            if trusted is None:
                _require(meta["release_id"] == release_id, "release_scope_mismatch")
            manifest = trusted or (_read_manifest(_manifest_path(settings, release_id))
                                   if release_state != "normalized" else None)
            if manifest is not None:
                if trusted is None:
                    evidence = [e["evidence"] for e in row["guard_evidence"] if e["state"] == "validated"]
                    _require(manifest["release_id"] == release_id and len(evidence) == 1
                             and manifest_digest(manifest) == evidence[0].get("validated_manifest_sha256")
                             and manifest["files"] == evidence[0].get("validated_files"),
                             "validated_manifest_changed_rebuild_required")
                data_schema_version = manifest["data_schema_version"]
            if trusted is not None:
                # Published bytes are immutable ledger-bound evidence. Today's
                # parser/registry must not reinterpret historical raw envelopes.
                root = settings.snapshots_dir / release_id
                files = trusted['files']
            else:
                files = _verify_complete(root, meta, data_schema_version=data_schema_version)
            if manifest is not None:
                _verify_local(root, manifest)
        except Exception:
            raise PipelineError("query_release_output_unverified") from None
        verification = {"complete": True, "hashes_verified": True, "rows_verified": True,
                        "services": list(meta["services"]), "files_checked": len(files)}
    else:
        from .storage.active import ActiveSnapshotError, search_active
        try:
            _, result = search_active(settings, filters, limit=limit, cursor=cursor)
        except ActiveSnapshotError as exc:
            raise PipelineError(str(exc)) from None
        return result
    result = TransactionQuery(root, data_schema_version=data_schema_version).search(filters, limit=limit, cursor=cursor)
    result.update(release_state=release_state, verification=verification)
    return result
