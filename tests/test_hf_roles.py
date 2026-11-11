# SPDX-License-Identifier: GPL-3.0-only
"""Fast factory and scan-set checks; no remote access or integration fixtures."""
import json

import pytest

from kapa_runtime import pipeline
from kapa_runtime.config import RuntimeSettings
from kapa_runtime.credentials.loader import CredentialError, ENV_NAMES
from kapa_runtime.releases.hf import PublishError, scan_secret_payload
from test_hf_publish import FakeHfApi


@pytest.fixture
def settings(tmp_path, monkeypatch):
    for name in ENV_NAMES.values():
        monkeypatch.delenv(name, raising=False)
    config = tmp_path / "runtime.json"
    config.write_text(json.dumps({
        "workspace": ".", "hf_namespace": "owner", "hf_repos": {"market": "data"},
        "credential_files": {"hugging_face_read": "read.fake", "hugging_face_write": "write.fake"},
    }))
    (tmp_path / "read.fake").write_text("TESTKEY-read")
    (tmp_path / "write.fake").write_text("TESTKEY-write")
    monkeypatch.setenv("DATA_GO_KR_SERVICE_KEY", "TESTKEY-provider")
    monkeypatch.setattr(pipeline, "_policy", lambda _: {})
    return RuntimeSettings.load(config)


def test_factory_selects_explicit_roles_even_with_injected_api(settings):
    publisher = pipeline._publisher(settings, FakeHfApi())
    assert publisher._token == "TESTKEY-write"
    assert publisher._read_token == "TESTKEY-read"
    reader = pipeline._publisher(settings, FakeHfApi(), read_only=True)
    assert reader._token == reader._read_token == "TESTKEY-read"


def test_read_only_factory_needs_no_write_alias(settings):
    settings.credential_files.pop("hugging_face_write")
    reader = pipeline._publisher(settings, FakeHfApi(), read_only=True)
    assert reader._read_token == "TESTKEY-read"


def test_real_api_factory_receives_only_read_token_for_read_only(settings, monkeypatch):
    received = []

    def fake_api(*, token):
        received.append(token)
        return FakeHfApi()

    monkeypatch.setattr(pipeline, "HfApi", fake_api)
    pipeline._publisher(settings, read_only=True)
    assert received == ["TESTKEY-read"]


@pytest.mark.parametrize("source", ["file", "env"])
def test_diagnose_reports_stale_legacy_with_valid_roles(settings, tmp_path, monkeypatch, capsys, source):
    from kapa_runtime.cli import main

    if source == "file":
        settings.credential_files["hugging_face"] = tmp_path / "legacy.fake"
        settings.credential_files["hugging_face"].write_text("KEY=TESTKEY-stale")
    else:
        monkeypatch.setenv("HF_TOKEN", "KEY=TESTKEY-stale")
    monkeypatch.setattr(RuntimeSettings, "load", lambda _: settings)

    assert main(["diagnose"]) == 1
    output = capsys.readouterr().out
    diagnostics = {item["alias"]: item for item in json.loads(output)["credentials"]}
    assert set(diagnostics) == set(pipeline._secret_aliases(settings, hf=True))
    assert diagnostics["hugging_face"] == {
        "alias": "hugging_face", "source": source, "status": "error",
        "error_code": "label_form", "usage": ["secret_scan"],
    }
    assert all(diagnostics[role]["status"] == "ok"
               for role in ("hugging_face_read", "hugging_face_write"))
    assert "TESTKEY" not in output
    with pytest.raises(CredentialError, match="label_form"):
        pipeline._loaded_secrets(settings, hf=True)


def test_diagnose_reports_env_only_legacy_role_usage(settings, monkeypatch, capsys):
    from kapa_runtime.cli import main

    settings.credential_files.clear()
    monkeypatch.setenv("HF_TOKEN", "TESTKEY-legacy")
    monkeypatch.setattr(RuntimeSettings, "load", lambda _: settings)

    assert main(["diagnose"]) == 0
    output = capsys.readouterr().out
    diagnostics = {item["alias"]: item for item in json.loads(output)["credentials"]}
    assert set(diagnostics) == set(pipeline._secret_aliases(settings, hf=True))
    assert diagnostics["hugging_face"]["source"] == "env"
    assert diagnostics["hugging_face"]["usage"] == [
        "secret_scan", "hugging_face_write", "hugging_face_read",
    ]
    assert diagnostics["hugging_face_read"]["warning"] == "legacy_hugging_face_used_for_read"
    assert "TESTKEY" not in output


@pytest.mark.parametrize("value", ["TESTKEY-provider", "TESTKEY-read", "TESTKEY-write", "TESTKEY-legacy"])
def test_every_configured_hf_alias_is_in_secret_scan(settings, monkeypatch, value):
    monkeypatch.setenv("HF_TOKEN", "TESTKEY-legacy")
    secrets = pipeline._loaded_secrets(settings, hf=True)
    with pytest.raises(PublishError):
        scan_secret_payload(value.encode(), "data.json", secrets)
