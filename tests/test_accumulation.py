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

REAL_PLAN = a._plan
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


@pytest.mark.parametrize('job', ['test-only-secret', 'prefix-test-only-secret'])
def test_job_identifier_secret_refused_before_persistence(setup, job):
    before = setup.state_db.read_bytes()
    with client(lambda _: pytest.fail('network before identifier validation')) as http:
        with pytest.raises(a.p.PipelineError) as error:
            a.collect_batch(setup, job_id=job, client=http, clock=lambda: NOW)
    assert str(error.value) == 'identifier_scan_failed'
    assert setup.state_db.read_bytes() == before
    assert not (setup.data_root / 'accumulation').exists()
    assert not (setup.snapshots_dir / job).exists()
    assert not (setup.manifests_dir / (job + '.json')).exists()


@pytest.mark.parametrize('service', a.SERVICES)
@pytest.mark.parametrize('budget', [None, True, '3', 3.0, 0, 2, 4, 151, 153])
def test_invalid_or_missing_pair_budget(setup, service, budget):
    path = setup.data_root / 'operator-policy.json'
    policy = json.loads(path.read_text())
    if budget is None:
        policy['services'][service].pop('max_requests')
    else:
        policy['services'][service]['max_requests'] = budget
    path.write_text(json.dumps(policy))
    with client(lambda _: pytest.fail('network before budget validation')) as http:
        with pytest.raises(a.p.PipelineError, match='invalid_pair_budget'):
            a.collect_batch(setup, job_id='budget', client=http, clock=lambda: NOW)
    with StateStore(setup.state_db) as store:
        assert store._connection.execute('SELECT count(*) FROM collection_jobs').fetchone()[0] == 0
        assert store._connection.execute('SELECT count(*) FROM runs').fetchone()[0] == 0


@pytest.mark.parametrize('service', a.SERVICES)
@pytest.mark.parametrize('basis', [None, '', ' \t\n', True, 1, ['review'], {'review': True}, 'x' * 4097])
def test_invalid_terms_before_collection(setup, service, basis):
    path = setup.data_root / 'operator-policy.json'
    policy = json.loads(path.read_text())
    policy['services'][service]['terms_basis'] = basis
    path.write_text(json.dumps(policy))
    before = setup.state_db.read_bytes()
    with client(lambda _: pytest.fail('network before terms validation')) as http:
        with pytest.raises(a.p.PipelineError, match='build_evidence_required'):
            a.collect_batch(setup, job_id='terms', client=http, clock=lambda: NOW)
    assert setup.state_db.read_bytes() == before


@pytest.mark.parametrize('state,expected', [
    ('validated', 0), ('empty', 0), ('paused', 3), ('blocked', 1),
    ('running', 1), ('built', 1), ('planned', 1), ('partial', 1),
    ('failed', 1), ('quarantined', 1), ('unknown', 1), (None, 1),
])
def test_collect_batch_cli_exit_states(setup, monkeypatch, capsys, state, expected):
    from kapa_runtime import cli
    monkeypatch.setattr(cli.RuntimeSettings, 'load', lambda path: setup)
    monkeypatch.setattr(a, 'collect_batch', lambda *args, **kwargs: {'state': state})
    assert cli.main(['collect-batch', '--job-id', 'job', '--config', 'unused.json']) == expected
    assert json.loads(capsys.readouterr().out)['state'] == state


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


def test_unknown_provider_field_stops_as_schema_drift(setup):
    item = (b'<item><dealYear>2026</dealYear><dealMonth>11</dealMonth><dealDay>1</dealDay>'
            b'<futureProviderField>unexpected</futureProviderField></item>')
    payload = ZERO.replace(b'<items/>', b'<items>' + item + b'</items>').replace(
        b'<totalCount>0</totalCount>', b'<totalCount>1</totalCount>')
    with client(lambda _: httpx.Response(200, content=payload)) as http:
        result = a.collect_batch(setup, job_id='drift', client=http, clock=lambda: NOW)
    assert result['state'] == 'blocked'
    assert result['error_code'] == 'schema_blocked'


@pytest.mark.parametrize('where', ['active', 'release', 'snapshot_ledger', 'manifest', 'snapshot', 'staging'])
def test_job_collision_before_requests(setup, where):
    job = 'A' if where == 'active' else 'occupied'
    if where in ('manifest', 'snapshot', 'staging'):
        directory = {'manifest': setup.manifests_dir, 'snapshot': setup.snapshots_dir,
                     'staging': setup.staging_dir}[where]
        directory.mkdir(parents=True, exist_ok=True)
        (directory / (job + '.json' if where == 'manifest' else job)).touch()
    with StateStore(setup.state_db) as store:
        store.migrate_jobs()
        if where == 'release':
            store._connection.execute('INSERT INTO releases VALUES (?,?,?,?)',
                                      (job, 'failed', '[]', NOW.isoformat()))
        if where == 'snapshot_ledger':
            store._connection.execute('INSERT INTO snapshot_parents VALUES (?,?,?)', (job, 'A', 'a' * 64))
    with client(lambda _: pytest.fail('adapter called')) as http:
        with pytest.raises(a.p.PipelineError, match='job_id_collision'):
            a.collect_batch(setup, job_id=job, client=http, clock=lambda: NOW)
    with StateStore(setup.state_db) as store:
        assert store._connection.execute('SELECT count(*) FROM collection_jobs').fetchone()[0] == 0
        assert store._connection.execute('SELECT count(*) FROM runs').fetchone()[0] == 0


@pytest.mark.parametrize('limit,value', [('MAX_SNAPSHOT_ENTRIES', 8), ('MAX_METADATA_BYTES', 4096),
                                          ('MAX_SHARDS', 1)])
def test_manifest_capacity_before_adapter(setup, monkeypatch, limit, value):
    from kapa_runtime.releases import manifest_v2 as v2
    monkeypatch.setattr(v2, limit, value)
    calls = []
    with client(lambda _: (calls.append(1), httpx.Response(200, content=ZERO))[1]) as http:
        with pytest.raises(a.p.PipelineError, match='manifest_capacity_exceeded'):
            a.collect_batch(setup, job_id='capacity', client=http, clock=lambda: NOW)
    assert calls == []
    with StateStore(setup.state_db) as store:
        assert store._connection.execute('SELECT count(*) FROM runs').fetchone()[0] == 0
        assert store._connection.execute('SELECT count(*) FROM collection_jobs').fetchone()[0] == 0


@pytest.mark.parametrize('service', a.SERVICES)
def test_daily_limit_below_attempt_rejected(setup, service):
    path = setup.data_root / 'operator-policy.json'
    policy = json.loads(path.read_text())
    policy['services'][service]['daily_limit'] = 2
    path.write_text(json.dumps(policy))
    with client(lambda _: pytest.fail('adapter called')) as http:
        with pytest.raises(a.p.PipelineError, match='invalid_daily_quota'):
            a.collect_batch(setup, job_id='limit', client=http, clock=lambda: NOW)


@pytest.mark.parametrize('service', a.SERVICES)
def test_zero_capacity_pauses_and_resumes(setup, monkeypatch, service):
    # Restore the real planner (the fixture substitutes a fixed tiny plan).
    monkeypatch.setattr(a, '_plan', REAL_PLAN)
    monkeypatch.setattr(a, 'lawd_reference', lambda *_: (['11110'], 'test-reference'))
    with StateStore(setup.state_db) as store:
        store.record_run('exhausted', service_id=service, quota_day='2026-11-12', reserved_requests=9000)
    with client(lambda _: pytest.fail('adapter called')) as http:
        result = a.collect_batch(setup, job_id='paused', client=http, clock=lambda: NOW)
    assert result == {'state': 'paused', 'error_code': 'quota_pause', 'completed_pairs': 0}
    with client() as http:
        result = a.collect_batch(setup, job_id='paused', client=http,
                                 clock=lambda: datetime.fromisoformat('2026-11-13T12:00:00+09:00'))
    assert result['state'] == 'validated', result


def test_no_backlog_is_empty_even_without_quota(setup, monkeypatch):
    monkeypatch.setattr(a, '_plan', REAL_PLAN)
    monkeypatch.setattr(a, 'lawd_reference', lambda *_: ([], 'empty-reference'))
    with StateStore(setup.state_db) as store:
        for service in a.SERVICES:
            store.record_run(service, service_id=service, quota_day='2026-11-12', reserved_requests=9000)
    with client(lambda _: pytest.fail('adapter called')) as http:
        assert a.collect_batch(setup, job_id='empty', client=http, clock=lambda: NOW) == {
            'state': 'empty', 'pairs': 0}


def test_sharded_parent_materializes_and_retries(setup):
    from kapa_runtime.releases import manifest_v2 as v2
    parent = json.loads(setup.active_manifest.read_text())
    block = dict(schema_version='kapa-runtime-coverage/1',
                 license='SPDX-License-Identifier: GPL-3.0-only',
                 selected_partitions=parent['selected_partitions'], coverage=parent['coverage'])
    payload = json.dumps(block, sort_keys=True).encode()
    sha = hashlib.sha256(payload).hexdigest()
    name = f'manifests/coverage/{sha}.json'
    path = setup.snapshots_dir / 'A' / name
    path.parent.mkdir(parents=True)
    path.write_bytes(payload)
    ref = dict(path=name, sha256=sha, entries=8)
    parent.update(selected_partitions=[], coverage=v2.coverage_for_partitions([], []), coverage_shards=[ref])
    dump_manifest(parent, setup.active_manifest)
    dump_manifest(parent, setup.manifests_dir / 'A.json')
    with StateStore(setup.state_db) as store:
        evidence = [{'state': 'published', 'evidence': {
            'commit_reread': True, 'published_manifest_sha256': manifest_digest(parent)}}]
        store._connection.execute('UPDATE releases SET guard_evidence=? WHERE release_id=?',
                                  (json.dumps(evidence), 'A'))
    with client() as http:
        result = a.collect_batch(setup, job_id='sharded', client=http, clock=lambda: NOW)
    assert result['state'] == 'validated', result
    manifest = json.loads((setup.manifests_dir / 'sharded.json').read_text())
    assert ref in manifest['coverage_shards']
    assert (setup.snapshots_dir / 'sharded' / name).read_bytes() == payload
    from kapa_runtime.releases.manifest import dumps_manifest
    from kapa_runtime.storage.snapshot_query import SnapshotInventory
    from kapa_runtime.storage.query import SnapshotIntegrityError
    budget = len(dumps_manifest(manifest).encode('utf-8')) + sum(
        len((setup.snapshots_dir / 'sharded' / r['path']).read_bytes())
        for r in manifest['coverage_shards'])
    inventory = SnapshotInventory(setup.snapshots_dir / 'sharded', manifest, metadata_budget=budget)
    inventory.verify()
    assert inventory.metadata_bytes == budget
    with pytest.raises(SnapshotIntegrityError):
        SnapshotInventory(setup.snapshots_dir / 'sharded', manifest, metadata_budget=budget - 1)
    with client(lambda _: pytest.fail('built job recollected')) as http:
        assert a.collect_batch(setup, job_id='sharded', client=http, clock=lambda: NOW)['state'] == 'validated'


def test_asymmetric_allowances_two_page_rent(setup):
    path = setup.data_root / 'operator-policy.json'
    policy = json.loads(path.read_text())
    policy['services']['molit-apartment-rent']['max_requests'] = 6
    path.write_text(json.dumps(policy))
    calls = []
    item = b'<item><dealYear>2026</dealYear><dealMonth>11</dealMonth><dealDay>1</dealDay></item>'
    def respond(request):
        calls.append(request)
        if 'Rent' not in request.url.path:
            return httpx.Response(200, content=ZERO)
        page = int(request.url.params['pageNo'])
        payload = ZERO.replace(b'<items/>', b'<items>' + item * (1000 if page == 1 else 1) + b'</items>')
        payload = payload.replace(b'<totalCount>0</totalCount>', b'<totalCount>1001</totalCount>')
        payload = payload.replace(b'<pageNo>1</pageNo>', f'<pageNo>{page}</pageNo>'.encode())
        return httpx.Response(200, content=payload)
    with client(respond) as http:
        result = a.collect_batch(setup, job_id='asymmetric', client=http, clock=lambda: NOW)
    assert result['state'] == 'validated', result
    assert len(calls) == 6
    with StateStore(setup.state_db) as store:
        assert store.reserved_requests('molit-apartment-sale', '2026-11-12') == 6
        assert store.reserved_requests('molit-apartment-rent', '2026-11-12') == 12
    assert Budget(sale_requests=9, rent_requests=12, sale_requests_per_pair=3,
                  rent_requests_per_pair=6).pair_capacity == 2


@pytest.mark.parametrize('mocked', [False, True])
def test_dangling_job_manifest_refused(setup, monkeypatch, mocked):
    path = setup.manifests_dir / 'dangling.json'
    if mocked:
        original = Path.lstat
        def lstat(self, *args, **kwargs):
            if self == path:
                return original(setup.active_manifest)
            return original(self, *args, **kwargs)
        monkeypatch.setattr(Path, 'lstat', lstat)
    else:
        try:
            path.symlink_to(setup.workspace / 'missing-target')
        except OSError:
            pytest.skip('symlink creation unavailable')
    with client(lambda _: pytest.fail('collection before collision check')) as http:
        with pytest.raises(a.p.PipelineError, match='job_id_collision'):
            a.collect_batch(setup, job_id='dangling', client=http, clock=lambda: NOW)


def test_reparse_ancestor_lstat_guard(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from kapa_runtime.storage.parquet import _safe_path
    original = Path.lstat
    def lstat(self, *args, **kwargs):
        if self == tmp_path:
            return SimpleNamespace(st_mode=0, st_file_attributes=0x400)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, 'lstat', lstat)
    with pytest.raises(ValueError, match='reparse'):
        _safe_path(tmp_path / 'missing' / 'file')


def test_reused_parent_shard_with_undeclared_observations(setup):
    from kapa_runtime.releases.manifest_v2 import resolve_snapshot
    parent = json.loads(setup.active_manifest.read_text())
    import copy
    origin_digest = manifest_digest(parent)
    parts = copy.deepcopy(parent['selected_partitions'])
    current = copy.deepcopy(parts)
    for part in parts:
        part['origin_digest'] = origin_digest
    for part in current:
        part['producer_release_id'] = 'B'
        part['lawd_code'] = '11140'
        for file in part['files']:
            original = setup.snapshots_dir / 'A' / file['path']
            file['path'] = file['path'].replace('lawd_code=11110', 'lawd_code=11140')
            target = setup.snapshots_dir / 'B' / file['path']
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(original.read_bytes())
    for part in parts:
        for file in part['files']:
            target = setup.snapshots_dir / 'B' / file['path']
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((setup.snapshots_dir / 'A' / file['path']).read_bytes())
    block = dict(schema_version='kapa-runtime-coverage/1', selected_partitions=parts,
                 coverage=coverage_for_partitions(parts, []),
                 license='SPDX-License-Identifier: GPL-3.0-only')
    payload = json.dumps(block).encode()
    sha = hashlib.sha256(payload).hexdigest()
    name = f'manifests/coverage/{sha}.json'
    target = setup.snapshots_dir / 'B' / name
    target.parent.mkdir(parents=True)
    target.write_bytes(payload)
    parent.update(release_id='B', batch_scope={'declared_pairs': [dict(lawd_code='11140', deal_ymd='202510')]},
                  selected_partitions=current, coverage=coverage_for_partitions(current, [
                      {k: part[k] for k in ('lawd_code', 'deal_ymd', 'service_id')} for part in current]),
                  coverage_shards=[dict(path=name, sha256=sha, entries=6)])
    dump_manifest(parent, setup.active_manifest)
    dump_manifest(parent, setup.manifests_dir / 'B.json')
    with StateStore(setup.state_db) as store:
        store._connection.execute('INSERT INTO releases VALUES (?,?,?,?)',
            ('B', 'active', json.dumps([{'state': 'published', 'evidence': {'commit_reread': True,
             'published_manifest_sha256': manifest_digest(parent)}}]), NOW.isoformat()))
    with client() as http:
        result = a.collect_batch(setup, job_id='reuse-undeclared', client=http, clock=lambda: NOW)
    assert result['state'] == 'validated', result
    manifest = json.loads((setup.manifests_dir / 'reuse-undeclared.json').read_text())
    resolved = resolve_snapshot(manifest, lambda name: (setup.snapshots_dir / 'reuse-undeclared' / name).read_bytes())
    assert len(resolved['selected_partitions']) == 8
    assert parent['coverage_shards'][0] in manifest['coverage_shards']


def test_ownership_invariant_runs_before_collection(setup, monkeypatch):
    from kapa_runtime.releases import snapshot
    def reject(*args):
        raise ValueError('Pair must belong to a unique block')
    monkeypatch.setattr(snapshot, 'check_pair_ownership', reject)
    with client(lambda _: pytest.fail('network before ownership preflight')) as http:
        with pytest.raises(ValueError, match='unique block'):
            a.collect_batch(setup, job_id='ownership-preflight', client=http, clock=lambda: NOW)
    with StateStore(setup.state_db) as store:
        assert all(store.reserved_requests(s, '2026-11-12') == 0 for s in a.SERVICES)


def test_terms_metadata_limits_before_collection(setup):
    path = setup.data_root / 'operator-policy.json'
    policy = json.loads(path.read_text())
    policy['services'][a.SERVICES[0]]['review_note'] = 'x' * 257
    path.write_text(json.dumps(policy))
    before = setup.state_db.read_bytes()
    with client(lambda _: pytest.fail('network before metadata validation')) as http:
        with pytest.raises(a.p.PipelineError, match='build_evidence_required'):
            a.collect_batch(setup, job_id='terms-length', client=http, clock=lambda: NOW)
    assert setup.state_db.read_bytes() == before


def test_terms_basis_maximum_builds(setup):
    path = setup.data_root / 'operator-policy.json'
    policy = json.loads(path.read_text())
    for service in a.SERVICES:
        policy['services'][service]['terms_basis'] = 'x' * 4096
        policy['services'][service]['review_note'] = 'x' * 256
    path.write_text(json.dumps(policy))
    with client() as http:
        assert a.collect_batch(setup, job_id='terms-boundary', client=http,
                               clock=lambda: NOW)['state'] == 'validated'
