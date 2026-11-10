# SPDX-License-Identifier: GPL-3.0-only
import base64
import json
from pathlib import Path
from urllib.parse import quote, quote_plus

import httpx
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from kapa_runtime import pipeline as p
from kapa_runtime.config import RuntimeSettings
from kapa_runtime.credentials.loader import ENV_NAMES
from kapa_runtime.releases.hf import PublishError, scan_secret_payload
from kapa_runtime.storage.state import StateStore
from test_hf_publish import FakeHfApi, FakeSecret

FIXTURES = Path(__file__).parent / "fixtures" / "rtms"
KEY = "TESTKEY-not-real+/="
SHA = "a" * 40


@pytest.fixture
def settings(tmp_path, monkeypatch):
    for env in ENV_NAMES.values():
        monkeypatch.delenv(env, raising=False)
    for name in ("provider.fake", "hub.fake"):
        (tmp_path / name).write_text(KEY, encoding="utf-8")
    config = tmp_path / "runtime.json"
    config.write_text(json.dumps({"workspace": ".", "data_root": "runtime",
        "credential_files": {"data_go_kr": "provider.fake", "hugging_face": "hub.fake"},
        "hf_namespace": "owner", "hf_repos": {"market": "data"}}))
    result = RuntimeSettings.load(config)
    result.data_root.mkdir()
    (result.data_root / "operator-policy.json").write_text(json.dumps({
        "services": {s: {"max_requests": 3, "terms_reviewed": True, "terms_basis": "synthetic fixture permission; reviewed_at=2026-11-10"} for s in p.SERVICES},
        "fixture_evidence": {"github_revision": SHA, "passed": True}}))
    monkeypatch.setattr(p, "_git_revision", lambda: SHA)
    return result


def http(failure=None):
    def response(request):
        assert request.url.params["serviceKey"] == KEY
        name = failure or ("rent.xml" if "Rent" in request.url.path else "sale.xml")
        payload = (FIXTURES / name).read_bytes().replace(b"<numOfRows>1</numOfRows>", b"<numOfRows>1000</numOfRows>")
        return httpx.Response(200, content=payload)
    return httpx.Client(transport=httpx.MockTransport(response), follow_redirects=False)


def collected(settings, release="r1"):
    with http() as client:
        first = p.collect(settings, service=p.SERVICES[0], lawd_code="11110", deal_ymd="202510", release_id=release, client=client)
        assert first["state"] == "collecting"
        second = p.collect(settings, service=p.SERVICES[1], lawd_code="11110", deal_ymd="202510", release_id=release, client=client)
        assert second["state"] == "normalized"


def test_happy_path(settings):
    collected(settings)
    assert p.build(settings, release_id="r1")["state"] == "validated"
    dry = p.publish(settings, release_id="r1", dry_run=True)
    assert not any("raw/" in n or n == "collection.json" for n in dry["allowlist"])
    api = FakeHfApi()
    assert p.publish(settings, release_id="r1", api=api)["state"] == "published"
    assert set(api.files) == set(dry["allowlist"])
    assert p.activate(settings, release_id="r1", api=api)["state"] == "active"
    result = p.query(settings, filters={"lawd_code": "11110"})
    assert result["total"] == 2
    assert {r["transaction_type"] for r in result["rows"]} == {"sale", "rent"}
    for path in settings.data_root.rglob("*"):
        if path.is_file():
            contents = path.read_bytes()
            assert KEY.encode() not in contents
            assert quote(KEY, safe="").encode() not in contents
    with StateStore(settings.state_db) as store:
        states = [e["state"] for e in store.get_release("r1")["guard_evidence"]]
        assert states == ["draft", "collecting", "collected", "normalized", "validated", "uploaded-candidate", "published", "active"]
    # Active queries use the copied, verified snapshot, not mutable staging.
    next((settings.staging_dir / "r1").rglob("*.parquet")).write_bytes(b"corrupt")
    assert p.query(settings, filters={})["total"] == 2


def test_auth_failure(settings):
    with http("auth-error.xml") as client:
        result = p.collect(settings, service=p.SERVICES[0], lawd_code="11110", deal_ymd="202510", release_id="bad", client=client)
    assert result["state"] == "failed"
    with StateStore(settings.state_db) as store:
        assert store.get_release("bad")["state"] == "failed"
    with pytest.raises(p.PipelineError):
        p.build(settings, release_id="bad")


def test_partial(settings):
    payload = (FIXTURES / "sale.xml").read_bytes().replace(b"<numOfRows>1</numOfRows>", b"<numOfRows>1000</numOfRows>").replace(b"<totalCount>1</totalCount>", b"<totalCount>1001</totalCount>")
    # A valid full first page, but the one-page budget prevents completion.
    item = payload[payload.index(b"<item>"):payload.index(b"</item>") + 7]
    payload = payload.replace(item, item * 1000)
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=payload))) as client:
        result = p.collect(settings, service=p.SERVICES[0], lawd_code="11110", deal_ymd="202510", release_id="partial", client=client)
    assert result["state"] == "partial"
    with StateStore(settings.state_db) as store:
        assert store.get_release("partial")["guard_evidence"][-1]["evidence"]["limited_scope"]


def test_scope_and_duplicate_rejected(settings):
    with http() as client:
        p.collect(settings, service=p.SERVICES[0], lawd_code="11110", deal_ymd="202510", release_id="r1", client=client)
        assert p.collect(settings, service=p.SERVICES[0], lawd_code="11110", deal_ymd="202510", release_id="r1", client=client)["recovered"]
        with pytest.raises(p.PipelineError):
            p.collect(settings, service=p.SERVICES[1], lawd_code="22220", deal_ymd="202510", release_id="r1", client=client)


def test_public_refusal_and_verify_failure_preserves_active(settings):
    collected(settings)
    p.build(settings, release_id="r1")
    with pytest.raises(PublishError):
        p.publish(settings, release_id="r1", api=FakeHfApi(private=False))
    api = FakeHfApi()
    p.publish(settings, release_id="r1", api=api)
    p.activate(settings, release_id="r1", api=api)
    old = settings.active_manifest.read_bytes()
    collected(settings, "r2")
    p.build(settings, release_id="r2")
    class Corrupt(FakeHfApi):
        def create_commit(self, *args, **kwargs):
            ref = super().create_commit(*args, **kwargs)
            self.files["README.md"] = b"bad"
            return ref
    with pytest.raises(p.PipelineError):
        p.publish(settings, release_id="r2", api=Corrupt())
    assert settings.active_manifest.read_bytes() == old
    with StateStore(settings.state_db) as store:
        assert store.get_release("r2")["state"] == "quarantined"


def test_rollback(settings):
    apis = {}
    for release in ("r1", "r2"):
        collected(settings, release)
        p.build(settings, release_id=release)
        api = FakeHfApi()
        apis[release] = api
        p.publish(settings, release_id=release, api=api)
        p.activate(settings, release_id=release, api=api)
    assert p.rollback(settings, api=apis["r1"])["release_id"] == "r1"
    assert p.query(settings, filters={})["total"] == 2


@pytest.mark.parametrize("role", ["market", "knowledge", "evals", "property", "matters"])
def test_visibility_config_role_restriction(tmp_path, role):
    from kapa_runtime.config import ConfigurationError
    config = tmp_path / "visibility.json"
    config.write_text(json.dumps({"workspace": ".", "hf_repos": {role: {"name": "data", "visibility": "public"}}}))
    if role == "market":
        assert RuntimeSettings.load(config).hf_visibilities[role] == "public"
    else:
        with pytest.raises(ConfigurationError):
            RuntimeSettings.load(config)


def test_public_market_explicit_and_mismatch(settings):
    from dataclasses import replace
    from types import SimpleNamespace
    from kapa_runtime.releases.hf import _HubWriter
    settings = replace(settings, hf_visibilities={"market": "public"})
    class PublicApi(FakeHfApi):
        def create_repo(self, repo_id, **kwargs):
            assert kwargs["private"] is False
            self.exists, self.private = True, False
        def create_commit(self, repo_id, *, operations, **kwargs):
            assert self.private is False
            self.files = {op.path_in_repo: op.path_or_fileobj for op in operations}
            return SimpleNamespace(oid="b" * 40)
    api = PublicApi(exists=False)
    publisher = _HubWriter(api, "owner", "data", expected_visibility="public", role="market", secret_values=lambda: [FakeSecret()], terms_evidence={"sale": {"terms_reviewed": True, "terms_basis": "synthetic"}})
    publisher.ensure_private_repo()
    assert api.private is False
    with pytest.raises(PublishError):
        _HubWriter(FakeHfApi(private=True), "owner", "data", expected_visibility="public", role="market", secret_values=lambda: [FakeSecret()], terms_evidence={"sale": {"terms_reviewed": True, "terms_basis": "synthetic"}}).ensure_private_repo()
    collected(settings)
    p.build(settings, release_id="r1")
    card = (settings.staging_dir / "r1" / "README.md").read_text(encoding="utf-8")
    assert card.startswith('---\nlicense: other\n')
    import re
    from huggingface_hub import DatasetCard
    metadata = DatasetCard(card).data.to_dict()
    assert metadata['license'] == 'other'
    assert re.fullmatch(r'[a-z0-9-.]+', metadata['license_name'])
    assert metadata['license_name'] == 'data-go-kr-unrestricted'
    assert metadata['license_link'] == 'https://www.data.go.kr/ugs/selectPortalPolicyView.do'
    assert metadata['language'] == ['ko']
    assert metadata['tags'] == ['real-estate', 'transactions', 'korea']
    assert '이용허락범위 제한 없음' in card.split('## 출처', 1)[1]
    assert "Private" not in card
    for expected in ("## 출처", "15126469/openapi.do", "15126474/openapi.do", "## 수집 범위",
                     "11110", "202510", "1행; fetched_at:", "## 스키마 요약", "decimal128",
                     "## 한계", "legal_verified=false", "## 재현성", "manifests/release.json",
                     "synthetic fixture permission"):
        assert expected in card
    assert str(settings.data_root) not in card and KEY not in card
    from test_public_promotion import MultiRepoApi
    api = MultiRepoApi()
    assert p.publish(settings, release_id="r1", api=api)["state"] == "published"
    p.activate(settings, release_id="r1", api=api)
    assert p.query(settings, filters={})["total"] == 2


def test_public_terms_rechecked_before_upload(settings):
    from dataclasses import replace
    settings = replace(settings, hf_visibilities={"market": "public"})
    collected(settings)
    p.build(settings, release_id="r1")
    path = settings.data_root / "operator-policy.json"
    policy = json.loads(path.read_text())
    policy["services"][p.SERVICES[0]]["terms_reviewed"] = False
    path.write_text(json.dumps(policy))
    with pytest.raises(p.PipelineError, match="terms_changed_rebuild_required"):
        p.publish(settings, release_id="r1", api=FakeHfApi(private=False))


def test_git_revision_refuses_dirty_repo(monkeypatch):
    from types import SimpleNamespace
    def git(command, **kwargs):
        assert "-C" in command
        return SimpleNamespace(stdout="?? local-change\n")
    monkeypatch.setattr(p.subprocess, "run", git)
    with pytest.raises(p.PipelineError, match="source_repository_dirty"):
        p._git_revision()


def test_daily_quota_reservations_are_per_service(settings):
    policy_path = settings.data_root / "operator-policy.json"
    policy = json.loads(policy_path.read_text())
    for entry in policy["services"].values():
        entry["daily_limit"] = 3
    policy_path.write_text(json.dumps(policy))
    collected(settings)
    with http() as client, pytest.raises(p.PipelineError, match="daily_quota_exceeded"):
        p.collect(settings, service=p.SERVICES[0], lawd_code="11110", deal_ymd="202510", release_id="over", client=client)


def test_evidence_required(settings):
    (settings.data_root / "operator-policy.json").unlink()
    with pytest.raises(p.PipelineError, match="operator_policy_required"):
        with http() as client:
            p.collect(settings, service=p.SERVICES[0], lawd_code="11110", deal_ymd="202510", client=client)


@pytest.mark.parametrize("encode", [lambda s: s.encode(), lambda s: quote(s, safe="").encode(),
    lambda s: quote_plus(s, safe="").encode(), lambda s: s.encode("utf-16-le"), lambda s: s.encode("utf-16-be"),
    lambda s: s.encode().hex().encode(), lambda s: s.encode().hex().upper().encode(),
    lambda s: base64.b64encode(s.encode()), lambda s: base64.urlsafe_b64encode(s.encode())])
def test_encoded_secrets(encode):
    class Secret:
        def reveal(self):
            return KEY
    with pytest.raises(PublishError, match="configured secret"):
        scan_secret_payload(b"prefix" + encode(KEY) + b"suffix", "data.json", [Secret()])


def test_compressed_parquet_secret(tmp_path):
    class Secret:
        def reveal(self):
            return KEY
    path = tmp_path / "hidden.parquet"
    from kapa_runtime.storage.parquet import SCHEMA
    row = {name: None for name in SCHEMA.names}
    row["extra"] = KEY
    pq.write_table(pa.Table.from_pylist([row] * 10000, schema=SCHEMA), path, compression="gzip", use_dictionary=False, write_statistics=False)
    assert KEY.encode() not in path.read_bytes()
    with pytest.raises(PublishError, match="configured secret"):
        scan_secret_payload(path.read_bytes(), path.name, [Secret()])


def test_portable_pipeline_cursor(settings):
    collected(settings)
    first = p.query(settings, filters={}, limit=1, release_id='r1')
    second = p.query(settings, filters={}, limit=1, release_id='r1', cursor=first['next_cursor'])
    assert first['rows'][0]['transaction_type'] != second['rows'][0]['transaction_type']
    assert second['next_cursor'] is None
    with pytest.raises(ValueError, match='invalid cursor'):
        p.query(settings, filters={'transaction_type': 'rent'}, limit=1, release_id='r1', cursor=first['next_cursor'])
    collected(settings, 'r2')
    with pytest.raises(ValueError, match='invalid cursor'):
        p.query(settings, filters={}, limit=1, release_id='r2', cursor=first['next_cursor'])


@pytest.mark.parametrize('form', [lambda s: s, lambda s: s.encode().hex(),
                                 lambda s: base64.b64encode(s.encode()).decode().rstrip('=')])
def test_all_credential_identifiers_rejected_before_writes(settings, form):
    hf = 'hf_AbCd0123456789abcdefghijklm'
    settings.credential_files['hugging_face'].write_text(hf)
    release = 'release-' + form(hf)
    with pytest.raises((PublishError, p.PipelineError)):
        with http() as client:
            p.collect(settings, service=p.SERVICES[0], lawd_code='11110', deal_ymd='202510', release_id=release, client=client)
    assert not settings.state_db.exists()
    assert not settings.staging_dir.exists()


def test_configured_invalid_other_credential_fails_closed(settings):
    settings.credential_files['hugging_face'].write_text('')
    with pytest.raises(Exception):
        with http() as client:
            p.collect(settings, service=p.SERVICES[0], lawd_code='11110', deal_ymd='202510', release_id='r1', client=client)
    assert not settings.state_db.exists() and not settings.staging_dir.exists()


def test_observation_identifier_checked_against_other_credentials(settings, monkeypatch):
    from dataclasses import replace
    value = 'hf_AbCd0123456789abcdef'
    settings.credential_files['hugging_face'].write_text(value)
    original = p.molit_rtms.collect
    def injected(*args, **kwargs):
        result = original(*args, **kwargs)
        return replace(result, observation=replace(result.observation, observation_id=value))
    monkeypatch.setattr(p.molit_rtms, 'collect', injected)
    with http() as client, pytest.raises(p.PipelineError):
        p.collect(settings, service=p.SERVICES[0], lawd_code='11110', deal_ymd='202510', release_id='r1', client=client)
    assert not (settings.staging_dir / 'r1' / 'raw').exists()
    assert value.encode() not in settings.state_db.read_bytes()


def test_activation_retry_preserves_previous(settings, monkeypatch):
    apis = {}
    for release in ('r1', 'r2'):
        collected(settings, release)
        p.build(settings, release_id=release)
        api = FakeHfApi()
        apis[release] = api
        p.publish(settings, release_id=release, api=api)
        if release == 'r1':
            p.activate(settings, release_id=release, api=api)
    transition = StateStore.transition_release
    def crash(self, release_id, state, guards):
        if state == 'active':
            raise RuntimeError('simulated crash after pointer replace')
        return transition(self, release_id, state, guards)
    monkeypatch.setattr(StateStore, 'transition_release', crash)
    with pytest.raises(RuntimeError):
        p.activate(settings, release_id='r2', api=api)
    previous = settings.active_manifest.with_name('previous.json').read_bytes()
    monkeypatch.setattr(StateStore, 'transition_release', transition)
    p.activate(settings, release_id='r2', api=api)
    assert settings.active_manifest.with_name('previous.json').read_bytes() == previous
    assert p.rollback(settings, api=apis["r1"])['release_id'] == 'r1'


@pytest.mark.parametrize('failure', ['snapshot_exists', 'verify_exception', 'stale_raw'])
def test_post_upload_failure_quarantines_and_retains_sha(settings, failure):
    collected(settings)
    p.build(settings, release_id='r1')
    if failure == 'snapshot_exists':
        (settings.snapshots_dir / 'r1').mkdir(parents=True)
    class Broken(FakeHfApi):
        def list_repo_files(self, *a, **k):
            if failure == 'verify_exception' and self.commits:
                raise OSError('synthetic network failure')
            return super().list_repo_files(*a, **k) + (['raw/stale.xml'] if failure == 'stale_raw' else [])
    with pytest.raises(p.PipelineError, match='post_upload_failed'):
        p.publish(settings, release_id='r1', api=Broken())
    with StateStore(settings.state_db) as store:
        row = store.get_release('r1')
        assert row['state'] == 'quarantined'
        assert row['guard_evidence'][-2]['evidence']['commit_sha'] == 'b' * 40
        assert row['guard_evidence'][-1]['evidence']['commit_sha'] == 'b' * 40


@pytest.mark.parametrize('new_day_full', [False, True])
def test_midnight_request_day_reservation(settings, new_day_full):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    before = datetime(2026, 11, 10, 23, 59, 59, tzinfo=ZoneInfo('Asia/Seoul'))
    after = datetime(2026, 11, 11, 0, 0, 1, tzinfo=ZoneInfo('Asia/Seoul'))
    if new_day_full:
        with StateStore(settings.state_db) as store:
            store.record_run('other-run', service_id=p.SERVICES[0], quota_day='2026-11-11', reserved_requests=10000)
    times = iter([before, before, after])
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(500) if len(calls) == 1 else httpx.Response(200, content=(FIXTURES / 'sale.xml').read_bytes().replace(b'<numOfRows>1', b'<numOfRows>1000'))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        if new_day_full:
            with pytest.raises(p.PipelineError):
                p.collect(settings, service=p.SERVICES[0], lawd_code='11110', deal_ymd='202510', release_id='r1', client=client, clock=lambda: next(times))
        else:
            p.collect(settings, service=p.SERVICES[0], lawd_code='11110', deal_ymd='202510', release_id='r1', client=client, clock=lambda: next(times))
    assert len(calls) == (1 if new_day_full else 2)
    with StateStore(settings.state_db) as store:
        assert store.reserved_requests(p.SERVICES[0], '2026-11-10') == 3
        assert store.reserved_requests(p.SERVICES[0], '2026-11-11') == (10000 if new_day_full else 3)


def test_configured_repo_secret_rejected_before_state(settings):
    from dataclasses import replace
    value = 'hf_AbCd0123456789abcdef'
    settings.credential_files['hugging_face'].write_text(value)
    settings = replace(settings, hf_namespace=value)
    with http() as client, pytest.raises(PublishError, match='configured secret'):
        p.collect(settings, service=p.SERVICES[0], lawd_code='11110', deal_ymd='202510', release_id='r1', client=client)
    assert not settings.state_db.exists() and not settings.staging_dir.exists()


def test_stale_remote_raw_preserves_previous_active(settings):
    collected(settings)
    p.build(settings, release_id='r1')
    api = FakeHfApi()
    p.publish(settings, release_id='r1', api=api)
    p.activate(settings, release_id='r1', api=api)
    old = settings.active_manifest.read_bytes()
    collected(settings, 'r2')
    p.build(settings, release_id='r2')
    class Stale(FakeHfApi):
        def list_repo_files(self, *a, **k):
            return super().list_repo_files(*a, **k) + ['raw/stale.xml']
    with pytest.raises(p.PipelineError):
        p.publish(settings, release_id='r2', api=Stale())
    assert settings.active_manifest.read_bytes() == old


class SimulatedProcessExit(BaseException):
    pass


@pytest.mark.parametrize('transition_to', ['collecting', 'collected', 'normalized'])
@pytest.mark.parametrize('corrupt', [False, True])
def test_collect_intermediate_crash_recovery(settings, monkeypatch, transition_to, corrupt):
    original = StateStore.transition_release
    def crash(self, release_id, state, evidence):
        if state == transition_to:
            raise SimulatedProcessExit()
        return original(self, release_id, state, evidence)
    monkeypatch.setattr(StateStore, 'transition_release', crash)
    with http() as client, pytest.raises(SimulatedProcessExit):
        p.collect(settings, service=p.SERVICES[0], lawd_code='11110', deal_ymd='202510', release_id='r1', client=client)
        p.collect(settings, service=p.SERVICES[1], lawd_code='11110', deal_ymd='202510', release_id='r1', client=client)
    monkeypatch.setattr(StateStore, 'transition_release', original)
    with pytest.raises(p.PipelineError, match='build_requires_normalized'):
        p.build(settings, release_id='r1')
    if corrupt and transition_to != 'collecting':
        next((settings.staging_dir / 'r1').rglob('*.parquet')).write_bytes(b'corrupt')
        with http() as client, pytest.raises(p.PipelineError, match='recovery_output_verification_failed'):
            p.collect(settings, service=p.SERVICES[1], lawd_code='11110', deal_ymd='202510', release_id='r1', client=client)
        with StateStore(settings.state_db) as store:
            assert store.get_release('r1')['state'] == 'failed'
        return
    with http() as client:
        if transition_to == 'collecting':
            p.collect(settings, service=p.SERVICES[0], lawd_code='11110', deal_ymd='202510', release_id='r1', client=client)
        result = p.collect(settings, service=p.SERVICES[1], lawd_code='11110', deal_ymd='202510', release_id='r1', client=client)
    assert result['state'] == 'normalized'
    assert p.build(settings, release_id='r1')['state'] == 'validated'


def test_interrupted_uploaded_candidate_becomes_quarantined(settings, monkeypatch):
    collected(settings)
    p.build(settings, release_id='r1')
    original = StateStore.transition_release
    def crash(self, release_id, state, evidence):
        result = original(self, release_id, state, evidence)
        if state == 'uploaded-candidate':
            raise SimulatedProcessExit()
        return result
    monkeypatch.setattr(StateStore, 'transition_release', crash)
    api = FakeHfApi()
    with pytest.raises(SimulatedProcessExit):
        p.publish(settings, release_id='r1', api=api)
    monkeypatch.setattr(StateStore, 'transition_release', original)
    with pytest.raises(p.PipelineError, match='interrupted_post_upload_quarantined'):
        p.publish(settings, release_id='r1', api=api)
    with StateStore(settings.state_db) as store:
        row = store.get_release('r1')
        assert row['state'] == 'quarantined'
        assert row['guard_evidence'][-1]['evidence']['commit_sha'] == 'b' * 40
    assert len(api.commits) == 1


def test_build_transition_crash_is_retryable(settings, monkeypatch):
    collected(settings)
    original = StateStore.transition_release
    def crash(self, release_id, state, evidence):
        if state == 'validated':
            raise SimulatedProcessExit()
        return original(self, release_id, state, evidence)
    monkeypatch.setattr(StateStore, 'transition_release', crash)
    with pytest.raises(SimulatedProcessExit):
        p.build(settings, release_id='r1')
    monkeypatch.setattr(StateStore, 'transition_release', original)
    assert p.build(settings, release_id='r1')['state'] == 'validated'
