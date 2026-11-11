# SPDX-License-Identifier: GPL-3.0-only
import os
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote, quote_plus

import pytest

from kapa_runtime.config import RuntimeSettings
from kapa_runtime.credentials import CredentialError, Secret, diagnose, load_secret, redact
from kapa_runtime.credentials import loader


@pytest.fixture
def settings(tmp_path):
    return RuntimeSettings(tmp_path, tmp_path / "data", {"rone": tmp_path / "fake-key.txt"})


@pytest.mark.parametrize("value", ["TESTKEY-not-real", "﻿TESTKEY-not-real\r\n", "TESTKEY-not-real\n\n", "\nTESTKEY-not-real\n"])
def test_file_formats(settings, value):
    settings.credential_files["rone"].write_bytes(value.encode("utf-8"))
    secret = load_secret("rone", settings, {})
    assert secret.reveal() == "TESTKEY-not-real"
    assert str(secret) == repr(secret) == "Secret(***)"
    assert diagnose("rone", settings, {}) == {"alias": "rone", "source": "file", "status": "ok", "error_code": None}


@pytest.mark.parametrize("value,code", [("", "empty"), ("\r\n", "empty"),
    ("TESTKEY-not-real\nTESTKEY-second", "multiple_lines"), ("NAME=TESTKEY-not-real", "label_form"),
    ("TESTKEY not-real", "invalid_whitespace"), (" TESTKEY-not-real", "invalid_whitespace"),
    ("TESTKEY-not-real\t", "invalid_whitespace"), ("TESTKEY-\x00not-real", "invalid_whitespace"),
    ("NAME=", "label_form"), ("TESTKEY-not-real\v", "invalid_whitespace")])
@pytest.mark.parametrize("source", ["env", "file"])
def test_invalid_values_no_leak_or_fallback(settings, value, code, source):
    settings.credential_files["rone"].write_text("TESTKEY-file-not-real", encoding="utf-8")
    env = {"RONE_API_KEY": value} if source == "env" else {}
    if source == "file":
        settings.credential_files["rone"].write_bytes(value.encode("utf-8"))
    with pytest.raises(CredentialError) as caught:
        load_secret("rone", settings, env)
    assert str(caught.value) == code
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    diagnostic = diagnose("rone", settings, env)
    assert diagnostic == {"alias": "rone", "source": source, "status": "error", "error_code": code}
    assert "TESTKEY" not in repr(diagnostic)


def test_env_wins_without_file_read(settings, monkeypatch):
    monkeypatch.setattr(loader, "_read_file", lambda _: pytest.fail("must not read file"))
    assert load_secret("rone", settings, {"RONE_API_KEY": "TESTKEY-env-not-real"}).reveal() == "TESTKEY-env-not-real"


def test_missing_and_unknown(settings):
    assert diagnose("rone", settings, {})["error_code"] == "missing_file"
    assert diagnose("rone", settings, {})["source"] == "missing"
    assert diagnose("vworld", settings, {})["error_code"] == "not_configured"
    assert diagnose("unknown", settings, {})["error_code"] == "unknown_alias"


def test_invalid_encoding_has_no_exception_chain(settings):
    settings.credential_files["rone"].write_bytes(b"TESTKEY-not-real\xff")
    with pytest.raises(CredentialError) as caught:
        load_secret("rone", settings, {})
    assert caught.value.error_code == "file_read_error"
    assert caught.value.__context__ is None


@pytest.mark.parametrize("parent", [False, True])
def test_symlinks_rejected(settings, tmp_path, parent):
    target = tmp_path / "target"
    target.mkdir()
    (target / "fake.txt").write_text("TESTKEY-not-real")
    link = tmp_path / "link"
    try:
        link.symlink_to(target if parent else target / "fake.txt", target_is_directory=parent)
    except OSError:
        pytest.skip("symlink creation unavailable")
    settings.credential_files["rone"] = link / "fake.txt" if parent else link
    assert diagnose("rone", settings, {})["error_code"] == "unsafe_link"


def test_windows_reparse_rejected(settings, monkeypatch):
    original = os.lstat
    leaf = settings.credential_files["rone"]
    leaf.write_text("TESTKEY-not-real")
    def fake(path):
        info = original(path)
        if Path(path) == leaf:
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
        return info
    monkeypatch.setattr(loader.os, "lstat", fake)
    assert diagnose("rone", settings, {})["error_code"] == "unsafe_link"


def test_directory_rejected(settings):
    settings.credential_files["rone"].mkdir()
    assert diagnose("rone", settings, {})["error_code"] == "not_regular_file"


def test_redact_all_forms():
    value = "TESTKEY-not-real /+=?"
    text = " | ".join((value, quote(value), quote(value, safe=""), quote_plus(value)))
    assert redact(text, [Secret(value)]) == "*** | *** | *** | ***"
    assert redact("unchanged", [Secret("")]) == "unchanged"
    assert redact("TESTKEY-long TESTKEY", [Secret("TESTKEY"), Secret("TESTKEY-long")]) == "*** ***"


@pytest.mark.parametrize("role,variable", [("hugging_face_write", "HF_TOKEN_WRITE"),
                                           ("hugging_face_read", "HF_TOKEN_READ")])
def test_hf_roles_legacy_fallback_and_explicit_precedence(settings, role, variable):
    legacy = {"HF_TOKEN": "TESTKEY-legacy"}
    assert load_secret(role, settings, legacy).reveal() == "TESTKEY-legacy"
    assert diagnose(role, settings, legacy)["warning"] == "legacy_" + role.replace("hugging_face_", "hugging_face_used_for_")
    env = {**legacy, variable: "TESTKEY-role"}
    assert load_secret(role, settings, env).reveal() == "TESTKEY-role"
    assert "warning" not in diagnose(role, settings, env)
    with pytest.raises(CredentialError):
        load_secret(role, settings, {**legacy, variable: ""})
    settings.credential_files[role] = settings.workspace / "role.fake"
    settings.credential_files[role].write_text("TESTKEY-file-role")
    assert load_secret(role, settings, legacy).reveal() == "TESTKEY-file-role"
    assert diagnose(role, settings, legacy)["source"] == "file"


def test_write_role_never_supplies_read_default(settings):
    with pytest.raises(CredentialError, match="not_configured"):
        load_secret("hugging_face_read", settings, {"HF_TOKEN_WRITE": "TESTKEY-write"})


def test_configured_missing_read_never_falls_back(settings):
    settings.credential_files["hugging_face_read"] = settings.workspace / "missing.fake"
    with pytest.raises(CredentialError, match="missing_file"):
        load_secret("hugging_face_read", settings, {"HF_TOKEN": "TESTKEY-legacy"})
