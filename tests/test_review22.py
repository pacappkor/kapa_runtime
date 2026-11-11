# SPDX-License-Identifier: GPL-3.0-only
from dataclasses import replace

import pytest


pytestmark = pytest.mark.slow
from huggingface_hub import CommitOperationDelete

from kapa_runtime import pipeline as p
from kapa_runtime.registry import ServiceRegistry
from kapa_runtime.releases.manifest import load_manifest, validate_manifest, ManifestError
from kapa_runtime.storage.state import StateStore
from test_pipeline import settings, collected, http
from test_public_promotion import MultiRepoApi, public_build


def test_public_tombstone_removes_staging_owned_partition(settings):
    settings = public_build(settings)
    api = MultiRepoApi()
    p.publish(settings, release_id='r1', api=api)
    missing = next(name for name in api.repos['owner/data']['files'] if name.endswith('.parquet'))
    # A different-scope publication supplies the current repository metadata;
    # the removed partition is retained ownership, not that candidate's file.
    with http() as client:
        for service in p.SERVICES:
            p.collect(settings, service=service, lawd_code='44440', deal_ymd='202510',
                      release_id='baseline', client=client)
    p.build(settings, release_id='baseline')
    p.publish(settings, release_id='baseline', api=api)
    api.create_commit('owner/data', operations=[CommitOperationDelete(path_in_repo=missing)])
    with StateStore(settings.state_db) as store:
        store._record_serving_removal('r1', missing, 'owner/data')
        assert missing in p._retained_owners(settings, store, 'owner/data-staging')[0]
    for release, region in [('r2', '22220'), ('r3', '33330')]:
        with http() as client:
            for service in p.SERVICES:
                p.collect(settings, service=service, lawd_code=region, deal_ymd='202510',
                          release_id=release, client=client)
        p.build(settings, release_id=release)
        assert p.publish(settings, release_id=release, api=api)['state'] == 'published'
        assert missing not in api.repos['owner/data']['files']
        assert missing not in api.repos['owner/data-staging']['files']
    # A newer trusted publication explicitly listing the path supersedes removal.
    collected(settings, 'r4')
    p.build(settings, release_id='r4')
    p.publish(settings, release_id='r4', api=api)
    assert missing in api.repos['owner/data']['files']


@pytest.mark.parametrize('active', [False, True])
def test_published_query_does_not_replay_current_allowlist(settings, monkeypatch, active):
    collected(settings)
    p.build(settings, release_id='r1')
    api = MultiRepoApi()
    p.publish(settings, release_id='r1', api=api)
    if active:
        p.activate(settings, release_id='r1', api=api)
    before = p.query(settings, release_id='r1', filters={})['rows']
    manifest = load_manifest(p._manifest_path(settings, 'r1'))
    registry = ServiceRegistry.load()
    assert manifest['parser_version'] == p.molit_rtms.PARSER_VERSION
    assert manifest['normalized_extra_fields'] == {
        service: sorted(registry.get(service).normalized_extra_fields) for service in p.SERVICES}
    changed = ServiceRegistry([replace(spec, normalized_extra_fields=())
                               for spec in registry.services.values()])
    monkeypatch.setattr(ServiceRegistry, 'load', classmethod(lambda cls: changed))
    def no_replay(*args, **kwargs):
        pytest.fail('published query replayed normalization')
    monkeypatch.setattr(p, 'normalize', no_replay)
    assert p.query(settings, release_id='r1', filters={})['rows'] == before
    # No collection/raw metadata is needed after publication.
    p._metadata(settings, 'r1').unlink()
    assert p.query(settings, release_id='r1', filters={})['rows'] == before
    partition = next((settings.snapshots_dir / 'r1').rglob('*.parquet'))
    partition.write_bytes(b'corrupt')
    with pytest.raises(p.PipelineError, match='query_release_output_unverified'):
        p.query(settings, release_id='r1', filters={})


@pytest.mark.parametrize('allowlists', [None, [], {'other': []},
                                      {'molit-apartment-sale': ['x', 'x'], 'molit-apartment-rent': []},
                                      {'molit-apartment-sale': [1], 'molit-apartment-rent': []}])
def test_manifest_rejects_invalid_extra_field_audit(settings, allowlists):
    collected(settings)
    p.build(settings, release_id='r1')
    manifest = load_manifest(p._manifest_path(settings, 'r1'))
    manifest['normalized_extra_fields'] = allowlists
    with pytest.raises(ManifestError, match='allowlists'):
        validate_manifest(manifest)
    del manifest['normalized_extra_fields']
    assert validate_manifest(manifest) == manifest
