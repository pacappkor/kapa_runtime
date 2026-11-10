# SPDX-License-Identifier: GPL-3.0-only
import json

import pytest

from kapa_runtime import pipeline as p
from kapa_runtime.config import RuntimeSettings, ConfigurationError
from kapa_runtime.releases.hf import _HubWriter, PublishError
from kapa_runtime.storage.state import StateStore
from test_pipeline import settings, SimulatedProcessExit
from test_public_promotion import MultiRepoApi, public_build


@pytest.mark.parametrize('repos', [
    {'market': {'name': 'data', 'staging_name': 'candidate'}, 'knowledge': 'DATA'},
    {'knowledge': 'reference', 'evals': 'REFERENCE'},
])
def test_all_repo_aliases_rejected(tmp_path, repos):
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'workspace': '.', 'hf_repos': repos}))
    with pytest.raises(ConfigurationError):
        RuntimeSettings.load(config)


def pending_promotion(settings, api):
    api.failure = 'promote_crash'
    with pytest.raises(SimulatedProcessExit):
        p.publish(settings, release_id='r1', api=api)
    with StateStore(settings.state_db) as store:
        pending = store.promotion('r1')
        pending['public_commit_sha'] = api.repos['owner/data']['sha']
        pending['restore_payloads'] = {'safe.json': b'safe'.hex()}
        store.save_promotion('r1', pending)


def test_recovery_refuses_external_head(settings):
    settings = public_build(settings)
    api = MultiRepoApi()
    pending_promotion(settings, api)
    before = len(api.commits)
    api.repos['owner/data']['sha'] = 'e' * 40
    api.repos['owner/data']['files']['external.json'] = b'external'
    with pytest.raises(PublishError, match='head changed'):
        p.activate(settings, release_id='r1', api=api)
    assert len(api.commits) == before
    assert api.repos['owner/data']['files']['external.json'] == b'external'
    with StateStore(settings.state_db) as store:
        assert store.promotion('r1') is not None


def test_restoration_post_verify_head_change_retains_journal(settings):
    settings = public_build(settings)
    api = MultiRepoApi()
    pending_promotion(settings, api)
    original = api.hf_hub_download
    def race(repo, name, **kwargs):
        result = original(repo, name, **kwargs)
        if repo == 'owner/data' and name == 'safe.json':
            api.repos[repo]['sha'] = 'f' * 40
            api.repos[repo]['files']['external.json'] = b'external'
        return result
    api.hf_hub_download = race
    with pytest.raises(PublishError, match='head changed'):
        p.activate(settings, release_id='r1', api=api)
    with StateStore(settings.state_db) as store:
        pending = store.promotion('r1')
        assert pending is not None
        assert pending['public_commit_sha'] == api.commits[-1][1]
        assert store.get_release('r1')['state'] == 'validated'


def test_publication_post_verify_head_change_not_published(settings, monkeypatch):
    settings = public_build(settings)
    api = MultiRepoApi()
    original = _HubWriter.verify
    def race(self, *args, **kwargs):
        report = original(self, *args, **kwargs)
        if self.repo_id == 'owner/data' and report.passed:
            api.repos[self.repo_id]['sha'] = 'e' * 40
            api.repos[self.repo_id]['files']['external.json'] = b'external'
        return report
    monkeypatch.setattr(_HubWriter, 'verify', race)
    with pytest.raises(PublishError, match='head changed'):
        p.publish(settings, release_id='r1', api=api)
    with StateStore(settings.state_db) as store:
        assert store.get_release('r1')['state'] == 'uploaded-candidate'
        assert store.promotion('r1') is not None
    assert api.repos['owner/data']['files']['external.json'] == b'external'
