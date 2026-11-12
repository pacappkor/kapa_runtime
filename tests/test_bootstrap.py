# SPDX-License-Identifier: GPL-3.0-only
import json

import pytest

from kapa_runtime import pipeline as p, accumulation as a
from kapa_runtime.releases.bootstrap import bootstrap_cumulative
from kapa_runtime.releases.batch import publish_batch
from kapa_runtime.storage.state import StateStore
from test_pipeline import settings
from test_public_promotion import public_build, MultiRepoApi
from test_accumulation import NOW, client
from kapa_runtime.planning import plan_day, Budget, ServiceRange


@pytest.fixture
def legacy(settings):
    settings = public_build(settings)
    api = MultiRepoApi()
    p.publish(settings, release_id='r1', api=api)
    p.activate(settings, release_id='r1', api=api)
    return settings, api


@pytest.mark.parametrize('dry_run', [False, True])
@pytest.mark.parametrize('state', [None, 'preparing', 'built'])
@pytest.mark.parametrize('identifier', ['release', 'import'])
def test_secret_identifier_rejected_before_bootstrap_writes(settings, dry_run, state, identifier):
    from kapa_runtime.releases.hf import PublishError
    secret = 'TESTKEY-bootstrap-not-real'
    settings.credential_files['hugging_face'].write_text(secret, encoding='utf-8')
    tainted = 'prefix-' + secret + '-suffix'
    release_id = tainted if identifier == 'release' else 'boot'
    imports = [tainted] if identifier == 'import' else []
    # Existing frozen jobs must be checked before either preparation or publication
    # retries can consume them, even after the configured credentials change.
    frozen = json.dumps(dict(kind='bootstrap', explicit_imports=imports))
    with StateStore(settings.state_db) as store:
        store.migrate_jobs()
        if state is not None:
            store._connection.execute('INSERT INTO collection_jobs VALUES (?,?,?)',
                                      (release_id, state, frozen))
        before = store._connection.execute('SELECT * FROM collection_jobs').fetchall()
    with pytest.raises(PublishError, match='configured secret'):
        bootstrap_cumulative(settings, release_id=release_id,
                             import_release=imports, dry_run=dry_run)
    with StateStore(settings.state_db) as store:
        assert store._connection.execute('SELECT * FROM collection_jobs').fetchall() == before
    assert not settings.snapshots_dir.exists()
    assert not settings.manifests_dir.exists()
    assert not settings.cache_dir.exists()


def test_rollout(legacy, monkeypatch):
    settings, api = legacy
    original = (settings.manifests_dir / 'r1.json').read_bytes()
    with StateStore(settings.state_db) as store:
        ledger = store.get_release('r1')
    preview = bootstrap_cumulative(settings, release_id='boot', dry_run=True, api=api)
    assert preview['partitions'] == 2 and preview['public']['ops'] > 0
    assert len(api.commits) == 2
    assert not (settings.manifests_dir / 'boot.json').exists()
    assert bootstrap_cumulative(settings, release_id='boot', api=api)['state'] == 'published'
    assert (settings.manifests_dir / 'r1.json').read_bytes() == original
    with StateStore(settings.state_db) as store:
        assert store.get_release('r1') == ledger
    p.activate(settings, release_id='boot', cumulative=True, api=api)
    assert p.query(settings, filters={'lawd_code': '11110'})['total'] == 2
    policy_path = settings.data_root / 'operator-policy.json'
    policy = json.loads(policy_path.read_text())
    policy['accumulation_smoke_verified'] = True
    policy_path.write_text(json.dumps(policy))
    original_plan = a._plan
    def small(settings, store, resolved, policy, clock):
        _, budget, reference = original_plan(settings, store, resolved, policy, clock)
        return plan_day(['11110'], today=NOW, coverage={}, sale=ServiceRange('202611'),
                        rent=ServiceRange('202611'), budget=Budget(requests_per_pair=3, batch_cap=1)), budget, reference
    monkeypatch.setattr(a, '_plan', small)
    with client() as http:
        assert a.collect_batch(settings, job_id='next', client=http, clock=lambda: NOW)['state'] == 'validated'
    assert publish_batch(settings, job_id='next', api=api)['state'] == 'published'
    p.activate(settings, release_id='next', cumulative=True, api=api)
    assert p.query(settings, filters={'lawd_code': '11110'})['total'] == 2
    from kapa_runtime.storage.active import search_active
    _, found = search_active(settings, {'lawd_code': '11110'})
    assert found['total'] == 2
    import importlib.util
    if importlib.util.find_spec('mcp'):
        import asyncio
        from mcp import Client
        from kapa_runtime.mcp.server import create_server
        async def search():
            async with Client(create_server(settings), mode='legacy') as client:
                result = await client.call_tool('search_transactions', {'filters': {'lawd_code': '11110'}})
                assert not result.is_error
                assert json.loads(result.content[0].text)['data']['total'] == 2
        asyncio.run(search())
    assert p.rollback(settings, cumulative=True, api=api)['release_id'] == 'boot'
    assert p.activate(settings, release_id='r1', api=api)['release_id'] == 'r1'


@pytest.mark.parametrize('case', ['digest', 'metadata', 'bytes', 'pending'])
def test_refuse(legacy, case):
    settings, api = legacy
    if case == 'digest':
        with StateStore(settings.state_db) as store:
            row = store.get_release('r1')
            for event in row['guard_evidence']:
                event['evidence'].pop('published_manifest_sha256', None)
            store._connection.execute('UPDATE releases SET guard_evidence=? WHERE release_id=?',
                                      (json.dumps(row['guard_evidence']), 'r1'))
        message = 'attest_legacy'
    elif case == 'metadata':
        p._metadata(settings, 'r1').unlink()
        message = 'reobserve'
    elif case == 'bytes':
        next((settings.snapshots_dir / 'r1').rglob('*.parquet')).write_bytes(b'tampered')
        message = 'hash_mismatch'
    else:
        with StateStore(settings.state_db) as store:
            store._connection.execute("UPDATE releases SET state='published' WHERE release_id='r1'")
        message = 'Published-but-not-active'
    with pytest.raises(ValueError, match=message):
        bootstrap_cumulative(settings, release_id='boot', dry_run=True, api=api)
    assert len(api.commits) == 2


def test_attested_digest(legacy):
    settings, api = legacy
    with StateStore(settings.state_db) as store:
        row = store.get_release('r1')
        for event in row['guard_evidence']:
            event['evidence'].pop('published_manifest_sha256', None)
        store._connection.execute('UPDATE releases SET guard_evidence=? WHERE release_id=?',
                                  (json.dumps(row['guard_evidence']), 'r1'))
    assert p.attest_legacy(settings, release_id='r1', api=api)['attested']
    assert bootstrap_cumulative(settings, release_id='boot', dry_run=True, api=api)['state'] == 'planned'


def test_explicit_import_requires_origin(legacy):
    settings, api = legacy
    with pytest.raises(ValueError, match='attest_legacy'):
        bootstrap_cumulative(settings, release_id='boot', import_release=['unknown'], api=api)
    assert len(api.commits) == 2


def test_scoped_plan(legacy):
    settings, api = legacy
    bootstrap_cumulative(settings, release_id='boot', api=api)
    p.activate(settings, release_id='boot', cumulative=True, api=api)
    path = settings.data_root / 'operator-policy.json'
    policy = json.loads(path.read_text())
    policy['accumulation_scope'] = dict(lawd_codes=['11110'], start='202611', end='202611', batch_cap=1)
    path.write_text(json.dumps(policy))
    with StateStore(settings.state_db) as store:
        _, resolved, _ = a._parent(settings, store)
        daily, _, _ = a._plan(settings, store, resolved, policy, lambda: NOW)
    assert [item.pair for item in daily.items] == [('11110', '202611')]
    policy['accumulation_scope']['lawd_codes'] = ['41110']
    with StateStore(settings.state_db) as store, pytest.raises(ValueError, match='invalid_accumulation_scope'):
        a._plan(settings, store, resolved, policy, lambda: NOW)


def test_explicit_retained_import(legacy):
    settings, api = legacy
    with client() as http:
        for service in p.SERVICES:
            p.collect(settings, service=service, lawd_code='11140', deal_ymd='202510',
                      release_id='r2', client=http)
    p.build(settings, release_id='r2')
    p.publish(settings, release_id='r2', api=api)
    p.activate(settings, release_id='r2', api=api)
    preview = bootstrap_cumulative(settings, release_id='boot', dry_run=True, api=api)
    assert preview['partitions'] == 2  # retained r1 on HF does not extend coverage
    preview = bootstrap_cumulative(settings, release_id='boot', import_release=['r1'], dry_run=True, api=api)
    assert preview['partitions'] == 4
    bootstrap_cumulative(settings, release_id='boot', import_release=['r1'], api=api)
    p.activate(settings, release_id='boot', cumulative=True, api=api)
    assert p.query(settings, filters={'lawd_code': '11110'})['total'] == 2


def test_bootstrap_resume_ambiguous_commit(legacy):
    settings, api = legacy
    original = api.create_commit
    fired = False
    def interrupt(repo, **kwargs):
        nonlocal fired
        result = original(repo, **kwargs)
        if repo == 'owner/data-staging' and not fired:
            fired = True
            raise KeyboardInterrupt()
        return result
    api.create_commit = interrupt
    with pytest.raises(KeyboardInterrupt):
        bootstrap_cumulative(settings, release_id='boot', api=api)
    assert bootstrap_cumulative(settings, release_id='boot', api=api)['state'] == 'published'
    assert len(api.commits) == 4


def test_origin_observation_tamper(legacy):
    settings, api = legacy
    meta_path = p._metadata(settings, 'r1')
    meta = json.loads(meta_path.read_text())
    meta['completed'][p.SERVICES[0]]['run_id'] = 'invented'
    meta_path.write_text(json.dumps(meta))
    with pytest.raises(ValueError, match='reobserve'):
        bootstrap_cumulative(settings, release_id='boot', api=api)
    assert len(api.commits) == 2

@pytest.mark.parametrize('boundary', ['recorded', 'mkdir', 'materialized', 'renamed', 'manifest', 'built'])
def test_preparation_crash_resume(legacy, monkeypatch, boundary):
    from pathlib import Path
    from kapa_runtime.releases import bootstrap as b
    settings, api = legacy
    original_prepare = b._prepare_snapshot
    original_materialize = b.materialize
    original_rename = Path.rename
    original_write = b._atomic_write
    def crash():
        raise KeyboardInterrupt()
    def prepare(*args):
        if boundary == 'recorded':
            crash()
        original_prepare(*args)
        if boundary == 'built':
            crash()
    def materialize(m, root, load, **kwargs):
        if boundary == 'mkdir':
            root.mkdir(parents=True)
            (root / 'partial').write_bytes(b'incomplete')
            crash()
        result = original_materialize(m, root, load, **kwargs)
        if boundary == 'materialized':
            crash()
        return result
    def rename(path, target):
        result = original_rename(path, target)
        if boundary == 'renamed':
            crash()
        return result
    def write(path, payload):
        original_write(path, payload)
        if boundary == 'manifest':
            crash()
    with monkeypatch.context() as patch:
        patch.setattr(b, '_prepare_snapshot', prepare)
        patch.setattr(b, 'materialize', materialize)
        patch.setattr(Path, 'rename', rename)
        patch.setattr(b, '_atomic_write', write)
        with pytest.raises(KeyboardInterrupt):
            bootstrap_cumulative(settings, release_id='boot', api=api)
    with StateStore(settings.state_db) as store:
        state, raw = store._connection.execute(
            "SELECT state,data FROM collection_jobs WHERE job_id='boot'").fetchone()
    data = b._frozen_inputs(settings, json.loads(raw))
    assert 'https://' not in raw
    assert state == ('built' if boundary == 'built' else 'preparing')
    # Simulate an unclean process exit leaving an unrelated partial temp tree.
    leftover = settings.snapshots_dir / 'bootstrap-preparing-abandoned'
    leftover.mkdir()
    (leftover / 'partial').write_bytes(b'untrusted')
    assert bootstrap_cumulative(settings, release_id='boot', api=api)['state'] == 'published'
    final = p._read_manifest(settings.manifests_dir / 'boot.json')
    assert final['created_at'] == data['created_at']
    assert final['selected_partitions'] == data['manifest']['selected_partitions']
    assert (leftover / 'partial').read_bytes() == b'untrusted'
    assert len(api.commits) == 4


def test_frozen_bootstrap_blob_corruption_refused(legacy, monkeypatch):
    from kapa_runtime.releases import bootstrap as b
    from kapa_runtime.releases.hf import PublishError
    settings, api = legacy
    with monkeypatch.context() as patch:
        def crash(*args):
            raise KeyboardInterrupt()
        patch.setattr(b, '_prepare_snapshot', crash)
        with pytest.raises(KeyboardInterrupt):
            bootstrap_cumulative(settings, release_id='boot', api=api)
    with StateStore(settings.state_db) as store:
        raw = store._connection.execute(
            "SELECT data FROM collection_jobs WHERE job_id='boot'").fetchone()[0]
    descriptor = json.loads(raw)['inputs_blob']
    blob = settings.state_db.parent / 'publication-spool' / descriptor['sha256']
    payload = blob.read_bytes()
    blob.write_bytes(b' ' + payload[1:])  # Same size, different digest.
    with pytest.raises(PublishError, match='digest'):
        bootstrap_cumulative(settings, release_id='boot', api=api)
    assert not (settings.snapshots_dir / 'boot').exists()
    assert len(api.commits) == 2


@pytest.mark.parametrize('code', ['workspace_dependency_mismatch', 'arbitrary sensitive text'])
def test_bootstrap_workspace_error_codes(legacy, monkeypatch, code):
    settings, api = legacy
    def fail(settings):
        raise p.workspace_guard.WorkspaceError(code)
    monkeypatch.setattr(p.workspace_guard, 'capture', fail)
    with pytest.raises(p.PipelineError) as error:
        bootstrap_cumulative(settings, release_id='boot', api=api)
    expected = code if code == 'workspace_dependency_mismatch' else 'workspace_revision_unavailable'
    assert error.value.error_code == expected
    assert len(api.commits) == 2


@pytest.mark.parametrize('state', ['quarantined', 'failed', 'collected', 'validated'])
@pytest.mark.parametrize('role', ['parent', 'import', 'retry'])
def test_ineligible_origin_state(legacy, monkeypatch, state, role):
    from kapa_runtime.releases import bootstrap as b
    settings, api = legacy
    if role == 'import':
        with client() as http:
            for service in p.SERVICES:
                p.collect(settings, service=service, lawd_code='11140', deal_ymd='202510',
                          release_id='r2', client=http)
        p.build(settings, release_id='r2')
        p.publish(settings, release_id='r2', api=api)
        p.activate(settings, release_id='r2', api=api)
    if role == 'retry':
        with monkeypatch.context() as patch:
            def crash(*args):
                raise KeyboardInterrupt()
            patch.setattr(b, '_prepare_snapshot', crash)
            with pytest.raises(KeyboardInterrupt):
                bootstrap_cumulative(settings, release_id='boot', api=api)
    with StateStore(settings.state_db) as store:
        store._connection.execute("UPDATE releases SET state=? WHERE release_id='r1'", (state,))
    with pytest.raises(ValueError, match='legacy_serving_state_required'):
        bootstrap_cumulative(settings, release_id='boot',
                             import_release=['r1'] if role == 'import' else None, api=api)


def test_bootstrap_creation_time(legacy):
    from datetime import datetime, timezone
    settings, api = legacy
    parent = p._read_manifest(settings.active_manifest)
    before = datetime.now(timezone.utc)
    bootstrap_cumulative(settings, release_id='boot', api=api)
    after = datetime.now(timezone.utc)
    manifest = p._read_manifest(settings.manifests_dir / 'boot.json')
    assert before <= datetime.fromisoformat(manifest['created_at']) <= after
    assert manifest['created_at'] != parent['created_at']
    meta = json.loads(p._metadata(settings, 'r1').read_text())
    assert {v['service_id']: v['fetched_at'] for v in manifest['selected_partitions']} == {
        service: v['fetched_at'] for service, v in meta['completed'].items()}


@pytest.mark.parametrize('start,end,empty', [
    ('200001', '201201', False), ('200601', '201012', True),
    ('199001', '200001', True), ('202701', '202801', True)])
def test_scope_intersects_history(legacy, monkeypatch, start, end, empty):
    settings, _ = legacy
    policy = p._policy(settings)
    policy['accumulation_scope'] = dict(lawd_codes=['11110'], start=start, end=end, batch_cap=1)
    original = a.plan_day
    calls = []
    def capture(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)
    monkeypatch.setattr(a, 'plan_day', capture)
    with StateStore(settings.state_db) as store:
        store.migrate_jobs()
        if empty:
            with pytest.raises(ValueError, match='accumulation_scope_empty_service_range'):
                a._plan(settings, store, {'selected_partitions': []}, policy, lambda: NOW)
            assert not calls
        else:
            daily, _, _ = a._plan(settings, store, {'selected_partitions': []}, policy, lambda: NOW)
            assert calls[0]['sale'] == ServiceRange('200601', '201201')
            assert calls[0]['rent'] == ServiceRange('201101', '201201')
            assert all('201101' <= item.deal_ymd <= '201201' for item in daily.items)
