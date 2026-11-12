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
