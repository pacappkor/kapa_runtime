# SPDX-License-Identifier: GPL-3.0-only
from datetime import datetime
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from kapa_runtime import accumulation as a
from kapa_runtime.config import RuntimeSettings
from kapa_runtime.credentials import Secret
from kapa_runtime.planning import Budget, plan_day, ServiceRange
from kapa_runtime.releases.manifest import dump_manifest, manifest_digest
from kapa_runtime.releases.manifest_v2 import coverage_for_partitions
from kapa_runtime.storage.parquet import write_partition
from kapa_runtime.storage.state import StateStore
from test_manifest_v2 import root, partitions

NOW = datetime.fromisoformat('2026-11-12T12:00:00+09:00')
ZERO = b'<response><header><resultCode>000</resultCode></header><body><items/><totalCount>0</totalCount><pageNo>1</pageNo><numOfRows>1000</numOfRows></body></response>'


@pytest.fixture
def setup(tmp_path, monkeypatch):
    settings = RuntimeSettings(tmp_path, tmp_path / 'runtime', required_repos=())
    settings.manifests_dir.mkdir(parents=True)
    parent = root()
    parent['files'] = []
    parent['selected_partitions'] = parts = partitions(zero=True)
    parent['coverage'] = coverage_for_partitions(parts, [{k: v[k] for k in ('lawd_code', 'deal_ymd', 'service_id')} for v in parts])
    parent_root = settings.snapshots_dir / 'A'
    for part in parts:
        scratch = tmp_path / part['service_id']
        f = write_partition([], scratch, asset_type='apartment', transaction_type=part['service_id'].rsplit('-', 1)[1],
                            lawd_code='11110', contract_year=2025, contract_month=10)[0]
        target = parent_root / part['files'][0]['path']
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f.path.read_bytes())
        part['files'][0]['sha256'] = f.sha256
    dump_manifest(parent, settings.active_manifest)
    dump_manifest(parent, settings.manifests_dir / 'A.json')
    with StateStore(settings.state_db) as store:
        store._connection.execute('INSERT INTO releases VALUES (?,?,?,?)', ('A', 'active', json.dumps([
            {'state': 'published', 'evidence': {'commit_reread': True, 'published_manifest_sha256': manifest_digest(parent)}}]), NOW.isoformat()))
    policy = {'accumulation_smoke_verified': True, 'services': {s: {'max_requests': 3, 'terms_reviewed': True, 'terms_basis': 'fixture'} for s in a.SERVICES},
              'fixture_evidence': {'passed': True, 'github_revision': 'a' * 40}}
    (settings.data_root / 'operator-policy.json').write_text(json.dumps(policy))
    monkeypatch.setattr(a.p, '_loaded_secrets', lambda settings: [Secret('test-only-secret')])
    monkeypatch.setattr(a.p, 'load_secret', lambda *args: Secret('test-only-secret'))
    monkeypatch.setattr(a.p, '_git_revision', lambda settings: 'a' * 40)
    monkeypatch.setattr(a.p.workspace_guard, 'capture', lambda settings: ({}, []))
    original = a._plan
    def small(settings, store, resolved, policy, clock):
        _, budget, reference = original(settings, store, resolved, policy, clock)
        daily = plan_day(['11110', '11140'], today=NOW, coverage={},
                         sale=ServiceRange('202611'), rent=ServiceRange('202611'),
                         budget=Budget(requests_per_pair=3, batch_cap=2))
        return daily, budget, reference
    monkeypatch.setattr(a, '_plan', small)
    return settings


def client(response=None):
    return httpx.Client(transport=httpx.MockTransport(response or (lambda request: httpx.Response(200, content=ZERO))))


def test_multi_pair_build(setup):
    with client() as http:
        result = a.collect_batch(setup, job_id='batch', client=http, clock=lambda: NOW)
    assert result['state'] == 'validated', result
    assert result['pairs'] == 2
    with StateStore(setup.state_db) as store:
        assert store._connection.execute('PRAGMA user_version').fetchone()[0] == 4
        assert store._connection.execute("SELECT count(*) FROM attempts WHERE state='complete'").fetchone()[0] == 4
        assert store.reserved_requests(a.SERVICES[0], '2026-11-12') == 6
    assert json.loads(setup.active_manifest.read_text())['release_id'] == 'A'
    assert a.collect_batch(setup, job_id='batch', client=None, clock=lambda: NOW)['state'] == 'validated'


def test_dry_run_and_lock(setup, monkeypatch):
    monkeypatch.setattr(a.p, '_loaded_secrets', lambda *_: pytest.fail('credentials accessed'))
    assert a.collect_batch(setup, dry_run=True, clock=lambda: NOW)['reservations_per_service'] == 6
    with StateStore(setup.state_db):
        with pytest.raises(RuntimeError, match='lock'):
            a.plan(setup, clock=lambda: NOW)
    with StateStore(setup.state_db) as store:
        assert store.reserved_requests(a.SERVICES[0], '2026-11-12') == 0


@pytest.mark.parametrize('body,code', [(b'<response><header><resultCode>30</resultCode></header></response>', 'auth_blocked'),
                                       (b'<wrong/>', 'schema_blocked')])
def test_permanent_stop(setup, body, code):
    calls = []
    def respond(request):
        calls.append(1)
        return httpx.Response(200, content=body)
    with client(respond) as http:
        result = a.collect_batch(setup, job_id='bad', client=http, clock=lambda: NOW)
    assert result['error_code'] == code
    assert len(calls) == 1
    with pytest.raises(a.p.PipelineError, match='operator_intervention'):
        a.collect_batch(setup, job_id='tomorrow', clock=lambda: NOW)


def test_crash_resume_reuses_sibling(setup):
    calls = []
    def crash(request):
        calls.append(1)
        if len(calls) == 2:
            raise KeyboardInterrupt()
        return httpx.Response(200, content=ZERO)
    with client(crash) as http, pytest.raises(KeyboardInterrupt):
        a.collect_batch(setup, job_id='resume', client=http, clock=lambda: NOW)
    resumed = []
    with client(lambda request: (resumed.append(1), httpx.Response(200, content=ZERO))[1]) as http:
        result = a.collect_batch(setup, job_id='resume', client=http, clock=lambda: NOW)
    assert result['state'] == 'validated', result
    assert len(resumed) == 3
    with StateStore(setup.state_db) as store:
        assert store._connection.execute("SELECT count(*) FROM attempts WHERE state='interrupted'").fetchone()[0] == 1


def test_quota_mid_job(setup):
    with StateStore(setup.state_db) as store:
        for service in a.SERVICES:
            store.record_run(service, service_id=service, quota_day='2026-11-12', reserved_requests=8997)
    with client() as http:
        result = a.collect_batch(setup, job_id='quota', client=http, clock=lambda: NOW)
    assert result == {'state': 'paused', 'error_code': 'quota_pause', 'completed_pairs': 1}
    with StateStore(setup.state_db) as store:
        assert all(store.reserved_requests(s, '2026-11-12') == 9000 for s in a.SERVICES)


def test_midnight_reserves_both_services(setup):
    current = [NOW]
    def respond(request):
        current[0] = datetime.fromisoformat('2026-11-13T00:00:01+09:00')
        return httpx.Response(200, content=ZERO)
    with client(respond) as http:
        assert a.collect_batch(setup, job_id='midnight', client=http, clock=lambda: current[0])['state'] == 'validated'
    with StateStore(setup.state_db) as store:
        for service in a.SERVICES:
            assert store.reserved_requests(service, '2026-11-12') == 3
            assert store.reserved_requests(service, '2026-11-13') == 6


def test_batch_byte_cap(setup, monkeypatch):
    monkeypatch.setattr(a, 'MAX_BATCH_BYTES', 1)
    with client() as http:
        result = a.collect_batch(setup, job_id='cap', client=http, clock=lambda: NOW)
    assert result['error_code'] == 'batch_cap_exceeded'
    assert not (setup.manifests_dir / 'cap.json').exists()


def test_city_exclusion_and_default_ledger(tmp_path):
    excluded, binding = a.lawd_reference()
    all_codes, other = a.lawd_reference(False)
    codes = {v['code'] for v in excluded}
    assert '41110' not in codes and {'41111', '41113'} <= codes
    assert '36110' in codes
    assert len(excluded) < len(all_codes) and binding == other
    with StateStore(tmp_path / 'db') as store:
        assert store._connection.execute('PRAGMA user_version').fetchone()[0] == 2
        store.migrate_jobs()
    with StateStore(tmp_path / 'db') as store:
        assert store._connection.execute('PRAGMA user_version').fetchone()[0] == 4


def test_quota_response_no_retry(setup):
    calls = []
    with client(lambda request: (calls.append(1), httpx.Response(429))[1]) as http:
        result = a.collect_batch(setup, job_id='rate', client=http, clock=lambda: NOW)
    assert result['state'] == 'paused'
    assert len(calls) == 1


def test_reused_sibling_corruption_stops(setup):
    count = [0]
    def respond(request):
        count[0] += 1
        if count[0] == 2:
            raise KeyboardInterrupt()
        return httpx.Response(200, content=ZERO)
    with client(respond) as http, pytest.raises(KeyboardInterrupt):
        a.collect_batch(setup, job_id='corrupt', client=http, clock=lambda: NOW)
    file = next((setup.data_root / 'accumulation').rglob('*.parquet'))
    file.write_bytes(b'broken')
    with client(lambda _: pytest.fail('network after corruption')) as http:
        result = a.collect_batch(setup, job_id='corrupt', client=http, clock=lambda: NOW)
    assert result['state'] == 'blocked'


def test_partial_never_completes(setup):
    # A single full page promises a second page beyond the three-request cap.
    item = b'<item><dealYear>2026</dealYear><dealMonth>11</dealMonth><dealDay>1</dealDay></item>'
    payload = ZERO.replace(b'<items/>', b'<items>' + item * 1000 + b'</items>').replace(
        b'<totalCount>0</totalCount>', b'<totalCount>1001</totalCount>')
    with client(lambda _: httpx.Response(200, content=payload)) as http:
        result = a.collect_batch(setup, job_id='partial', client=http, clock=lambda: NOW)
    assert result['error_code'] == 'partition_blocked'
    with StateStore(setup.state_db) as store:
        assert store._connection.execute("SELECT count(*) FROM attempts WHERE state='partial'").fetchone()[0] == 1
        assert store._connection.execute("SELECT count(*) FROM work_items WHERE state='complete'").fetchone()[0] == 0


def test_quota_pair_atomic_and_no_stale_steal(tmp_path):
    path = tmp_path / 'ledger'
    with StateStore(path) as store:
        store.record_run('rent-full', service_id=a.SERVICES[1], quota_day='2026-11-12', reserved_requests=9000)
        with pytest.raises(a.p.PipelineError, match='quota_pause'):
            a._reserve(store, 'j', '11110', '202611', 150, '2026-11-12')
        assert store.reserved_requests(a.SERVICES[0], '2026-11-12') == 0
    path.with_name('ledger.lock').write_text('99999999')
    with pytest.raises(RuntimeError, match='lock'):
        StateStore(path)
    assert path.with_name('ledger.lock').exists()


def test_smoke_gate_and_plan_no_credential(setup, monkeypatch):
    policy = setup.data_root / 'operator-policy.json'
    value = json.loads(policy.read_text())
    value.pop('accumulation_smoke_verified')
    policy.write_text(json.dumps(value))
    monkeypatch.setattr(a.p, '_loaded_secrets', lambda *_: pytest.fail('secret read'))
    with pytest.raises(a.p.PipelineError, match='operator_smoke_required'):
        a.collect_batch(setup, job_id='gate')
    assert a.collect_batch(setup, dry_run=True, clock=lambda: NOW)['state'] == 'planned'


def test_pair_cap():
    with pytest.raises(ValueError, match='60'):
        Budget(batch_cap=61)
    daily = plan_day(['11110', '11140'], today=NOW, coverage={})
    assert len(daily.items) <= 60


def test_secret_scan_failure_blocks(setup, monkeypatch):
    def refuse(*args):
        raise ValueError('synthetic private content must never be logged')
    monkeypatch.setattr(a, 'scan_secret_payload', refuse)
    with client() as http:
        result = a.collect_batch(setup, job_id='scan', client=http, clock=lambda: NOW)
    assert result['state'] == 'blocked'
    assert 'synthetic' not in json.dumps(result)


def test_opt_in_cli_dry_run(setup, monkeypatch, capsys):
    from kapa_runtime import cli
    monkeypatch.setattr(cli.RuntimeSettings, 'load', lambda path: setup)
    monkeypatch.setattr(cli, 'diagnose', lambda *_: pytest.fail('credential diagnostic'))
    monkeypatch.setattr(a, 'plan', lambda settings, **kwargs: {'state': 'planned', 'reservations_per_service': 6})
    assert cli.main(['plan', '--config', 'unused.json']) == 0
    assert json.loads(capsys.readouterr().out)['state'] == 'planned'
    assert cli.main(['collect-batch', '--job-id', 'dry', '--dry-run', '--config', 'unused.json']) == 0
    assert json.loads(capsys.readouterr().out)['reservations_per_service'] == 6


def test_snapshot_record_retry_and_conflict(tmp_path):
    import copy
    manifest = root()
    with StateStore(tmp_path / 'db') as store:
        store.record_snapshot(manifest)
        store.record_snapshot(manifest)
        changed = copy.deepcopy(manifest)
        changed['selected_partitions'][0]['run_id'] = 'different'
        with pytest.raises(ValueError, match='conflict'):
            store.record_snapshot(changed)


def test_crash_after_snapshot_record_rebuilds_without_requests(setup, monkeypatch):
    original = a.materialize
    monkeypatch.setattr(a, 'materialize', lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()))
    with client() as http, pytest.raises(KeyboardInterrupt):
        a.collect_batch(setup, job_id='freeze', client=http, clock=lambda: NOW)
    monkeypatch.setattr(a, 'materialize', original)
    with client(lambda _: pytest.fail('completed pair recollected')) as http:
        result = a.collect_batch(setup, job_id='freeze', client=http, clock=lambda: NOW)
    assert result['state'] == 'validated', result


def test_different_policy_cannot_resume(setup):
    with client(lambda _: (_ for _ in ()).throw(KeyboardInterrupt())) as http, pytest.raises(KeyboardInterrupt):
        a.collect_batch(setup, job_id='bound', client=http, clock=lambda: NOW)
    policy = setup.data_root / 'operator-policy.json'
    value = json.loads(policy.read_text())
    value['services'][a.SERVICES[0]]['max_requests'] = 6
    policy.write_text(json.dumps(value))
    with pytest.raises(a.p.PipelineError, match='job_binding_changed'):
        a.collect_batch(setup, job_id='bound', clock=lambda: NOW)
