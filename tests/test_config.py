# SPDX-License-Identifier: GPL-3.0-only
import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from kapa_runtime.config import ConfigurationError, RuntimeSettings


def test_relative_paths_and_derived(tmp_path):
    config = tmp_path / "runtime.json"
    config.write_text(json.dumps({"workspace": "work", "credential_files": {"rone": "fake.txt"},
                                 "hf_namespace": "realsolution", "hf_repos": {"market": "kapa-market-data"}}))
    settings = RuntimeSettings.load(config)
    assert settings.workspace == tmp_path / "work"
    assert settings.data_root == tmp_path / "work" / "_kapa_data"
    assert settings.credential_files == {"rone": tmp_path / "fake.txt"}
    assert settings.staging_dir == settings.data_root / "staging"
    assert settings.snapshots_dir == settings.data_root / "snapshots"
    assert settings.cache_dir == settings.data_root / "cache"
    assert settings.state_db == settings.data_root / "state" / "runtime.sqlite3"
    assert settings.active_manifest == settings.manifests_dir / "active.json"
    assert not settings.data_root.exists()
    with pytest.raises(FrozenInstanceError):
        settings.workspace = Path("other")


def test_env_path_and_explicit_override(tmp_path, monkeypatch):
    good = tmp_path / "good.json"
    good.write_text('{"workspace":".","data_root":"data"}')
    monkeypatch.setenv("KAPA_RUNTIME_CONFIG", str(good))
    assert RuntimeSettings.load().data_root == tmp_path / "data"
    monkeypatch.setenv("KAPA_RUNTIME_CONFIG", "missing.json")
    assert RuntimeSettings.load(good).workspace == tmp_path


@pytest.mark.parametrize("body", ['{}', '{', '[]', '{"workspace":".","workspace":"bad"}',
    '{"workspace":".","data_root":NaN}', '{"workspace":".","credential_files":{"unknown":"x"}}',
    '{"workspace":".","hf_repos":{"market":null}}', '{"workspace":42}'])
def test_bad_configs_are_sanitized(tmp_path, body):
    config = tmp_path / "bad.json"
    config.write_text(body)
    with pytest.raises(ConfigurationError) as caught:
        RuntimeSettings.load(config)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__


def test_missing_config(monkeypatch, tmp_path):
    monkeypatch.delenv("KAPA_RUNTIME_CONFIG", raising=False)
    with pytest.raises(ConfigurationError):
        RuntimeSettings.load()
    with pytest.raises(ConfigurationError):
        RuntimeSettings.load(tmp_path / "missing.json")


def test_design_shape(tmp_path):
    config = tmp_path / "design.json"
    config.write_text(json.dumps({"workspace_root": "..", "paths": {"data_root": "data"},
        "credentials": {"rone": {"fallback_file": "fake.txt"}},
        "hugging_face": {"namespace": "realsolution", "repositories": {"market": "kapa-market-data"}}}))
    assert RuntimeSettings.load(config).credential_files["rone"] == tmp_path / "fake.txt"
