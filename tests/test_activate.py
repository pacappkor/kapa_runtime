# SPDX-License-Identifier: GPL-3.0-only
import hashlib

import pytest

from kapa_runtime.releases import (
    ActivationError, VerificationReport, activate, build_manifest,
    dump_manifest, load_manifest, manifest_digest, rollback,
)


def manifest(release_id):
    return build_manifest(release_id=release_id, parser_version="1", state="published",
                          github_revisions={"owner/code": "a" * 40},
                          hugging_face_revisions=[{"repo_id": "owner/data", "commit_sha": "b" * 40}],
                          files=[{"path": "data.json", "sha256": hashlib.sha256(b"{}").hexdigest(), "rows": 0}])


def report(m, errors=()):
    return VerificationReport(manifest_digest(m), (("owner/data", "b" * 40),), ("data.json",), errors)


def test_activation_and_rollback(tmp_path):
    old, new = manifest("old"), manifest("new")
    active, candidate = tmp_path / "active.json", tmp_path / "candidate.json"
    dump_manifest(old, active)
    dump_manifest(new, candidate)
    data = tmp_path / "data.json"
    data.write_bytes(b"{}")
    activate(candidate, active, report(new))
    assert load_manifest(active)["release_id"] == "new"
    assert load_manifest(tmp_path / "previous.json")["release_id"] == "old"
    rollback(active, report(old))
    assert load_manifest(active) == old
    assert data.read_bytes() == b"{}"


@pytest.mark.parametrize("failure", ["partial", "wrong_manifest", "draft", "missing_repo", "missing_file"])
def test_failed_activation_preserves_active(tmp_path, failure):
    old, new = manifest("old"), manifest("new")
    active, candidate = tmp_path / "active.json", tmp_path / "candidate.json"
    dump_manifest(old, active)
    if failure == "draft":
        new["state"] = "draft"
    r = report(new, ("hash_mismatch",) if failure == "partial" else ())
    if failure == "wrong_manifest":
        r = report(old)
    elif failure == "missing_repo":
        new["hugging_face_revisions"].append({"repo_id": "owner/other", "commit_sha": "c" * 40})
        r = report(new)
    elif failure == "missing_file":
        r = VerificationReport(manifest_digest(new), r.revisions, ())
    dump_manifest(new, candidate)
    before = active.read_bytes()
    with pytest.raises(ActivationError):
        activate(candidate, active, r)
    assert active.read_bytes() == before
    assert not (tmp_path / "previous.json").exists()


def test_replace_failure_preserves_active(tmp_path, monkeypatch):
    import importlib
    module = importlib.import_module("kapa_runtime.releases.activate")
    old, new = manifest("old"), manifest("new")
    active, candidate = tmp_path / "active.json", tmp_path / "candidate.json"
    dump_manifest(old, active)
    dump_manifest(new, candidate)
    real_replace = module.os.replace

    def failing_replace(src, dst):
        if dst == active:
            raise OSError("simulated")
        return real_replace(src, dst)

    monkeypatch.setattr(module.os, "replace", failing_replace)
    with pytest.raises(OSError):
        activate(candidate, active, report(new))
    assert load_manifest(active) == old
    assert not list(tmp_path.glob(".release-*.tmp"))


@pytest.mark.parametrize('failure', ['missing', 'wrong_digest', 'partial'])
def test_rollback_requires_digest_bound_complete_report(tmp_path, failure):
    old, current = manifest('old'), manifest('current')
    active = tmp_path / 'active.json'
    dump_manifest(current, active)
    dump_manifest(old, tmp_path / 'previous.json')
    verified = None if failure == 'missing' else report(current if failure == 'wrong_digest' else old,
                                                       ('remote_failed',) if failure == 'partial' else ())
    before = active.read_bytes()
    with pytest.raises(ActivationError):
        rollback(active, verified)
    assert active.read_bytes() == before
