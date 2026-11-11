# SPDX-License-Identifier: GPL-3.0-only
import copy
import hashlib

import pytest

from kapa_runtime.releases.manifest import ManifestError, manifest_digest
from kapa_runtime.releases.snapshot import assemble_snapshot, check_parent, write_once
from kapa_runtime.storage.state import StateStore
from test_manifest_v2 import root, partitions


def assemble(parent, release, batch, origins, **kwargs):
    template = root()
    template['release_id'] = release
    return assemble_snapshot(template, parent, batch, origins=origins,
        trusted_digests={k: manifest_digest(v) for k, v in origins.items()}, enabled=True, **kwargs)


def test_chain_and_zero(tmp_path):
    a = root()
    b = assemble(a, 'B', partitions('B', '26110'), {'A': a})
    seoul = next(p for p in b['selected_partitions'] if p['lawd_code'] == '11110')
    assert seoul['producer_release_id'] == 'A'
    assert seoul['origin_digest'] == manifest_digest(a)
    c = assemble(b, 'C', partitions('C', zero=True), {'A': a, 'B': b})
    assert len(c['coverage']['observed']) == 4
    assert len(c['coverage']['zero']) == 2
    assert b['selected_partitions'][0]['observation_id'].startswith('A-')
    assert c['selected_partitions'][0]['previous_observation']['producer_release_id'] == 'A'
    with StateStore(tmp_path / 'state.db') as store:
        store.record_snapshot(b)
        assert store._connection.execute('PRAGMA user_version').fetchone()[0] == 3
        assert store._connection.execute('SELECT count(*) FROM partition_selections').fetchone()[0] == 4
    with StateStore(tmp_path / 'state.db') as store:
        assert store._connection.execute('PRAGMA user_version').fetchone()[0] == 3
        store.record_snapshot(c)
        assert store._connection.execute('SELECT count(*) FROM partition_selections').fetchone()[0] == 8


def test_conflict_and_tombstone(tmp_path):
    payload = b'old'
    name = 'data/observations/a/file.parquet'
    write_once(tmp_path, name, payload, hashlib.sha256(payload).hexdigest())
    write_once(tmp_path, name, payload, hashlib.sha256(payload).hexdigest())
    with pytest.raises(ManifestError, match='conflict'):
        write_once(tmp_path, name, b'new', hashlib.sha256(b'new').hexdigest())
    a = root()
    with pytest.raises(ManifestError, match='Tombstone'):
        assemble(a, 'B', partitions('B', '26110'), {'A': a},
                 tombstones=[a['selected_partitions'][0]['files'][0]['path']])
    batch = partitions('B', '26110')
    retained = copy.deepcopy(batch[0]['files'])
    retained[0]['sha256'] = 'f' * 64
    with pytest.raises(ManifestError, match='conflict'):
        assemble(a, 'B', batch, {'A': a}, retained=retained)


@pytest.mark.parametrize('field,value', [
    ('sha256', 'f' * 64), ('rows', 2), ('schema', 'different'),
    ('producer_release_id', 'other'), ('origin_digest', 'f' * 64),
])
@pytest.mark.parametrize('reverse', [False, True])
def test_retained_duplicate_conflicts(field, value, reverse):
    a = root()
    batch = partitions('B', '26110')
    entry = copy.deepcopy(batch[0]['files'][0])
    conflicting = {**entry, field: value}
    retained = [conflicting, entry] if reverse else [entry, conflicting]
    with pytest.raises(ManifestError, match='Conflicting retained path evidence'):
        assemble(a, 'B', batch, {'A': a}, retained=retained)


def test_identical_evidence_duplicates():
    a = root()
    batch = partitions('B', '26110')
    entry = batch[0]['files'][0]
    assert assemble(a, 'B', batch + [copy.deepcopy(batch[0])], {'A': a},
                    retained=[entry, copy.deepcopy(entry)]) == assemble(a, 'B', batch, {'A': a})


def test_batch_duplicates_checked_before_previous_observation_overwrite():
    a = root()
    batch = partitions('B')
    conflict = copy.deepcopy(batch[0])
    conflict['previous_observation'] = {'observation_id': 'forged'}
    with pytest.raises(ManifestError, match='Conflicting batch partition evidence'):
        assemble(a, 'B', batch + [conflict], {'A': a})


def test_parent_guards():
    a = root()
    class Ledger:
        def get_release(self, rid):
            return {'guard_evidence': [{'state': 'published', 'evidence': {
                'commit_reread': True, 'published_manifest_sha256': manifest_digest(a)}}]}
        def list_releases(self):
            return [{'state': 'published'}]
        def pending_promotions(self):
            return []
    with pytest.raises(ManifestError, match='Parent changed'):
        check_parent(Ledger(), {'release_id': 'A', 'digest': 'f' * 64}, a)
    with pytest.raises(ManifestError, match='Published-but-not-active'):
        check_parent(Ledger(), {'release_id': 'A', 'digest': manifest_digest(a)}, a)


def test_legacy_bootstrap():
    from kapa_runtime.releases.manifest import build_manifest
    parts = partitions('legacy')
    for p in parts:
        p['files'][0]['path'] = p['files'][0]['path'].replace(
            'observations/' + p['observation_id'] + '/', '')
    legacy = build_manifest(release_id='legacy', files=[f for p in parts for f in p['files']],
                            parser_version='parser/1')
    b = assemble(legacy, 'B', partitions('B', '26110'), {'legacy': legacy}, legacy_partitions=parts)
    assert b['selected_partitions'][0]['files'] == parts[0]['files']
    assert b['selected_partitions'][0]['producer_release_id'] == 'legacy'
    assert assemble(legacy, 'B', partitions('B', '26110'), {'legacy': legacy},
                    legacy_partitions=parts + [copy.deepcopy(parts[0])]) == b
    conflicting = copy.deepcopy(parts[0])
    conflicting['run_id'] = 'forged'
    with pytest.raises(ManifestError, match='Conflicting inherited partition evidence'):
        assemble(legacy, 'B', partitions('B', '26110'), {'legacy': legacy},
                 legacy_partitions=parts + [conflicting])


def test_rollback_after_reobservation(tmp_path):
    from kapa_runtime.releases.activate import activate, rollback
    from kapa_runtime.releases.hf import VerificationReport
    from kapa_runtime.releases.manifest import dump_manifest, resolve_snapshot_inventory, load_manifest
    a = root()
    b = assemble(a, 'B', partitions('B', '26110'), {'A': a})
    c = assemble(b, 'C', partitions('C'), {'A': a, 'B': b})
    active = tmp_path / 'active.json'
    reports = {}
    for m in (b, c):
        m['state'] = 'published'
        m['github_revisions'] = {'test/runtime': 'a' * 40}
        m['hugging_face_revisions'] = [{'repo_id': 'test/repo', 'commit_sha': 'a' * 40}]
        path = tmp_path / (m['release_id'] + '.json')
        dump_manifest(m, path)
        reports[m['release_id']] = VerificationReport(manifest_digest(m), (('test/repo', 'a' * 40),),
            tuple(f['path'] for f in resolve_snapshot_inventory(m, None)))
        activate(path, active, reports[m['release_id']])
    rollback(active, reports['B'])
    assert load_manifest(active)['release_id'] == 'B'


def test_retained_physical_owner_and_tombstone(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from kapa_runtime.pipeline import _retained_owners, _promotion_retained_files
    from kapa_runtime.releases.manifest import dump_manifest
    from kapa_runtime.storage.state import REQUIRED_GUARDS
    from kapa_runtime.models import ReleaseState
    settings = SimpleNamespace(snapshots_dir=tmp_path / 'snapshots',
        manifests_dir=tmp_path / 'manifests', active_manifest=tmp_path / 'active.json')
    settings.manifests_dir.mkdir()
    monkeypatch.setattr('kapa_runtime.pipeline._loaded_secrets', lambda *a, **k: [])
    a = root()
    a.update(state='published', github_revisions={'test/runtime': 'a' * 40},
             hugging_face_revisions=[{'repo_id': 'test/repo', 'commit_sha': 'b' * 40}])
    b = assemble(a, 'B', partitions('B', '26110'), {'A': a})
    b.update(state='published', github_revisions=a['github_revisions'],
             hugging_face_revisions=a['hugging_face_revisions'])
    c = assemble(b, 'C', partitions('C'), {'A': a, 'B': b})
    c.update(state='published', github_revisions=a['github_revisions'],
             hugging_face_revisions=a['hugging_face_revisions'])
    with StateStore(tmp_path / 'state.db') as store:
        for m in (a, b, c):
            # Same trusted publication history the pipeline consumes, no network.
            for state in list(ReleaseState)[:8]:
                evidence = {k: True for k in REQUIRED_GUARDS.get(state, ())}
                if state == ReleaseState.PUBLISHED:
                    evidence['published_manifest_sha256'] = manifest_digest(m)
                store.transition_release(m['release_id'], state, evidence)
            dump_manifest(m, settings.manifests_dir / (m['release_id'] + '.json'))
        dump_manifest(c, settings.active_manifest)
        owners, _ = _retained_owners(settings, store, 'test/repo')
        old = a['selected_partitions'][0]['files'][0]['path']
        assert owners[old][0] == 'A'
        assert owners[old][2] == a['selected_partitions'][0]['files'][0]
        from kapa_runtime.pipeline import _retained_repair
        from kapa_runtime.releases.hf import PublishError
        with pytest.raises(PublishError, match='immutable_retained'):
            _retained_repair(settings, store, 'test/repo', [])(old, [owners[old][2]])
        store._record_serving_removal('A', old, 'test/repo')
        tombstones = set()
        owners, _ = _retained_owners(settings, store, 'test/repo', tombstones=tombstones)
        assert old not in owners and old in tombstones
        assert old not in {f['path'] for f in _promotion_retained_files(
            settings, store, 'test/staging', 'test/repo')}


@pytest.mark.parametrize('withdrawn', [False, True])
@pytest.mark.parametrize('change', ['digest', 'producer', 'run'])
def test_retained_owner_conflicting_evidence(tmp_path, monkeypatch, withdrawn, change):
    from types import SimpleNamespace
    from kapa_runtime.pipeline import _retained_owners, PipelineError
    a = root()
    b = copy.deepcopy(a)
    b['release_id'] = 'B'
    if change == 'producer':
        for p in b['selected_partitions']:
            p['producer_release_id'] = 'B'
    else:
        b = assemble(a, 'B', partitions('B', '26110'), {'A': a})
        p = b['selected_partitions'][0]
        if change == 'digest':
            p['files'][0]['sha256'] = 'f' * 64
        else:
            p['run_id'] = 'forged'
    manifests = {'A': a, 'B': b}
    rows = []
    for rid, m in manifests.items():
        m['hugging_face_revisions'] = [{'repo_id': 'test/repo', 'commit_sha': 'a' * 40}]
    # Bind inherited partitions after the publication metadata is finalized.
    for p in b['selected_partitions']:
        if p['producer_release_id'] == 'A':
            p['origin_digest'] = manifest_digest(a)
    for rid, m in manifests.items():
        events = [{'state': 'published', 'at': rid, 'evidence': {
            'commit_reread': True, 'published_manifest_sha256': manifest_digest(m)}}]
        if withdrawn and rid == 'A':
            events.append({'state': 'published', 'at': 'A', 'evidence': {
                'reason': 'retained_partition_removed', 'repo_id': 'test/repo',
                'removed_path': a['selected_partitions'][0]['files'][0]['path']}})
        rows.append({'release_id': rid, 'state': 'published', 'guard_evidence': events})
    store = SimpleNamespace(list_releases=lambda: rows,
                            get_release=lambda rid: next(r for r in rows if r['release_id'] == rid))
    settings = SimpleNamespace(active_manifest=tmp_path / 'absent', snapshots_dir=tmp_path)
    monkeypatch.setattr('kapa_runtime.pipeline._trusted_published_manifest',
                        lambda settings, store, rid: manifests[rid])
    with pytest.raises(PipelineError, match='immutable_path_conflict|retained_origin_untrusted'):
        _retained_owners(settings, store, 'test/repo')


@pytest.mark.parametrize('conflict', [None, 'digest', 'schema', 'producer'])
def test_promotion_retained_duplicate_evidence(monkeypatch, conflict):
    from kapa_runtime.pipeline import _promotion_retained_files, PipelineError
    a = root()
    entry = a['selected_partitions'][0]['files'][0]
    owner = ('A', a, entry)
    other = copy.deepcopy(owner)
    if conflict == 'digest':
        other[2]['sha256'] = 'f' * 64
    elif conflict == 'schema':
        other[1]['data_schema_version'] = 'kapa-runtime-data/1'
    elif conflict == 'producer':
        other = ('B', other[1], other[2])
    monkeypatch.setattr('kapa_runtime.pipeline._retained_owners',
        lambda settings, store, repo, **kwargs: ({entry['path']: owner if repo == 'staging' else other}, None))
    if conflict:
        with pytest.raises(PipelineError, match='immutable_path_conflict'):
            _promotion_retained_files(None, None, 'staging', 'public')
    else:
        assert _promotion_retained_files(None, None, 'staging', 'public') == [
            dict(entry, data_schema_version=a['data_schema_version'])]


def test_materialize_checks_complete_inventory(tmp_path):
    from kapa_runtime.releases.snapshot import materialize
    from kapa_runtime.storage.parquet import write_partition
    m = root()
    payloads = {'README.md': b'readme'}
    m['files'][0]['sha256'] = hashlib.sha256(payloads['README.md']).hexdigest()
    for p in m['selected_partitions']:
        kind = p['service_id'].rsplit('-', 1)[1]
        written = write_partition([], tmp_path / 'source', asset_type='apartment',
            transaction_type=kind, lawd_code='11110', contract_year=2025, contract_month=10)[0]
        p.update(rows=0, status='zero')
        p['files'][0].update(rows=0, sha256=written.sha256)
        payloads[p['files'][0]['path']] = written.path.read_bytes()
    from kapa_runtime.releases.manifest_v2 import coverage_for_partitions, KEY
    m['coverage'] = coverage_for_partitions(m['selected_partitions'],
        [{k: p[k] for k in KEY} for p in m['selected_partitions']])
    materialize(m, tmp_path / 'snapshot', payloads.__getitem__, enabled=True)
    for name, payload in payloads.items():
        assert (tmp_path / 'snapshot' / name).read_bytes() == payload
    with pytest.raises(ManifestError, match='digest mismatch'):
        materialize(m, tmp_path / 'bad', lambda _: b'wrong', enabled=True)


def test_origin_tamper_and_disabled():
    a = root()
    b = assemble(a, 'B', partitions('B', '26110'), {'A': a})
    b['selected_partitions'][0]['run_id'] = 'forged'
    with pytest.raises(ManifestError, match='Origin partition'):
        assemble(b, 'C', partitions('C'), {'A': a, 'B': b})
    with pytest.raises(ManifestError, match='disabled'):
        assemble_snapshot(root(), a, [], origins={}, trusted_digests={})
