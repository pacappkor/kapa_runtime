# SPDX-License-Identifier: GPL-3.0-only
import json

import pytest

from kapa_runtime.cli import main
from kapa_runtime.credentials.loader import ENV_NAMES


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    for name in (*ENV_NAMES.values(), "KAPA_RUNTIME_CONFIG"):
        monkeypatch.delenv(name, raising=False)


def test_diagnose_only_synthetic_config(tmp_path, monkeypatch, capsys):
    config = tmp_path / "runtime.json"
    config.write_text('{"workspace":"."}')
    for name in ENV_NAMES.values():
        monkeypatch.setenv(name, "TESTKEY-not-real")
    assert main(["diagnose", "--config", str(config)]) == 0
    output = capsys.readouterr().out
    assert "TESTKEY" not in output
    parsed = json.loads(output)
    assert len(parsed["credentials"]) == 4
    assert all(item["source"] == "env" and item["status"] == "ok" for item in parsed["credentials"])


def test_diagnose_failures(tmp_path, capsys):
    config = tmp_path / "runtime.json"
    config.write_text('{"workspace":"."}')
    assert main(["diagnose", "--config", str(config)]) == 1
    assert all(item["source"] == "missing" for item in json.loads(capsys.readouterr().out)["credentials"])
    assert main(["diagnose"]) == 2
    assert json.loads(capsys.readouterr().out)["error_code"] == "configuration_error"


@pytest.mark.parametrize("name", ["collect", "build", "publish", "activate"])
def test_required_arguments(name, capsys):
    with pytest.raises(SystemExit) as caught:
        main([name])
    assert caught.value.code == 2
    assert "required" in capsys.readouterr().err


def test_collect_requires_release_id_before_dispatch(monkeypatch, capsys):
    from kapa_runtime import pipeline
    monkeypatch.setattr(pipeline, 'collect', lambda *a, **k: pytest.fail('dispatched invalid usage'))
    with pytest.raises(SystemExit) as caught:
        main(['collect', '--service', 'molit-apartment-sale', '--lawd-code', '11110', '--deal-ymd', '202510'])
    assert caught.value.code == 2
    output = capsys.readouterr()
    assert '--release-id' in output.err and 'required' in output.err
    assert output.out == ''


def test_collect_and_query_dispatch(tmp_path, monkeypatch, capsys):
    from kapa_runtime import pipeline
    config = tmp_path / "runtime.json"
    config.write_text('{"workspace":"."}')
    captured = {}
    def collect(settings, **kwargs):
        captured.update(kwargs)
        return {"release_id": "r1", "state": "collecting"}
    monkeypatch.setattr(pipeline, "collect", collect)
    assert main(["collect", "--config", str(config), "--service", "molit-apartment-sale",
                 "--lawd-code", "11110", "--deal-ymd", "202510", "--release-id", "r1"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "collecting"
    assert captured["lawd_code"] == "11110"
    def query(settings, **kwargs):
        assert kwargs["filters"]["price_krw_min"] == 10000
        assert kwargs["cursor"] == "portable-cursor"
        return {"rows": [], "total": 0}
    monkeypatch.setattr(pipeline, "query", query)
    assert main(["query", "--config", str(config), "--lawd-code", "11110", "--price-krw-min", "10000", "--cursor", "portable-cursor"]) == 0
    assert json.loads(capsys.readouterr().out)["total"] == 0


def test_exception_text_never_printed(tmp_path, monkeypatch, capsys):
    from kapa_runtime import pipeline
    config = tmp_path / "runtime.json"
    config.write_text('{"workspace":"."}')
    def fail(*a, **k):
        raise ValueError("TESTKEY-not-real https://example.invalid/?key=TESTKEY-not-real")
    monkeypatch.setattr(pipeline, "build", fail)
    assert main(["build", "--config", str(config), "--release-id", "r1"]) == 1
    output = capsys.readouterr().out
    assert json.loads(output)["error_code"] == "operation_failed"
    assert "TESTKEY" not in output and "https" not in output


def test_stdout_is_utf8_even_with_legacy_console(tmp_path, monkeypatch):
    import io
    from kapa_runtime import cli, pipeline
    config = tmp_path / "runtime.json"
    config.write_text('{"workspace":"."}')
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252")
    monkeypatch.setattr(cli.sys, "stdout", stream)
    monkeypatch.setattr(pipeline, "query", lambda *a, **k: {"rows": [{"building_name": "한글아파트"}]})
    assert main(["query", "--config", str(config), "--lawd-code", "11110"]) == 0
    stream.flush()
    payload = raw.getvalue()
    assert stream.encoding == "utf-8"
    assert "한글아파트".encode("utf-8") in payload
    assert b"\\\\u" not in payload
    assert json.loads(payload.decode("utf-8"))["rows"][0]["building_name"] == "한글아파트"
    stream.detach()


def test_publish_failure_reason_is_safe(tmp_path, monkeypatch, capsys):
    from kapa_runtime import pipeline
    from kapa_runtime.releases.hf import PublishError
    config = tmp_path / "runtime.json"
    config.write_text('{"workspace":"."}')
    def fail(*args, **kwargs):
        raise PublishError("safe failure", reason="hub_metadata_invalid") from None
    monkeypatch.setattr(pipeline, "publish", fail)
    assert main(["publish", "--config", str(config), "--release-id", "r1"]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "error", "error_code": "publish_error", "reason": "hub_metadata_invalid"}


def test_requires_command():
    with pytest.raises(SystemExit) as caught:
        main([])
    assert caught.value.code == 2
