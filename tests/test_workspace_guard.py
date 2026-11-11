# SPDX-License-Identifier: GPL-3.0-only
"""Synthetic Git siblings; no network and no real workspace mutations."""
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess

import pytest

from kapa_runtime import pipeline as p
from kapa_runtime.config import ConfigurationError, RuntimeSettings
from kapa_runtime.releases import workspace as w
from kapa_runtime.releases.manifest import load_manifest, validate_manifest
from test_pipeline import settings, collected
from test_hf_publish import FakeHfApi


SOURCE_GIT_REVISION = p._git_revision


def git(repo, *args):
    return subprocess.run(["git", "-c", "safe.directory=" + repo.absolute().as_posix(),
                           "-C", str(repo), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def commit(repo):
    git(repo, "add", ".")
    git(repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "-c", "commit.gpgsign=false", "commit", "-m", "fixture")
    return git(repo, "rev-parse", "HEAD")


def repo(root, name):
    path = root / name
    path.mkdir()
    git(path, "init")
    git(path, "config", "core.autocrlf", "false")
    (path / "README").write_text("fixture\n", encoding="utf-8")
    commit(path)
    return path


def combination(settings):
    skills = repo(settings.workspace, "kapa_skills")
    knowledge = repo(settings.workspace, "kapa_knowledge")
    head = git(skills, "rev-parse", "HEAD")
    (knowledge / "deps.lock").write_text(json.dumps({"kapa_skills": {
        "commit": head, "vendor": {"commit": head}}}), encoding="utf-8")
    commit(knowledge)
    return replace(settings, required_repos=("kapa_knowledge", "kapa_skills"))


def test_success_records_pins_and_unavailable_warnings(settings):
    settings = combination(settings)
    collected(settings)
    result = p.build(settings, release_id="r1")
    m = load_manifest(settings.manifests_dir / "r1.json")
    for name in settings.required_repos:
        assert m["github_revisions"]["pacappkor/" + name] == git(settings.workspace / name, "rev-parse", "HEAD")
    assert m["github_revisions"]["pacappkor/kapa_mcp"] is None
    assert len(result["warnings"]) == 3
    api = FakeHfApi()
    p.publish(settings, release_id="r1", api=api)
    validate_manifest(load_manifest(settings.manifests_dir / "r1.json"))
    p.activate(settings, release_id="r1", api=api)


@pytest.mark.parametrize("dirty", ["tracked", "untracked", "staged"])
def test_dirty_sibling_refuses_build(settings, dirty):
    settings = combination(settings)
    path = settings.workspace / "kapa_skills"
    (path / ("README" if dirty != "untracked" else "new-file")).write_text("changed")
    if dirty == "staged":
        git(path, "add", ".")
    collected(settings)
    with pytest.raises(p.PipelineError, match="workspace_repository_dirty"):
        p.build(settings, release_id="r1")


def test_missing_required_refuses_build(settings):
    settings = replace(settings, required_repos=("kapa_skills",))
    collected(settings)
    with pytest.raises(p.PipelineError, match="workspace_required_repository_unavailable"):
        p.build(settings, release_id="r1")


@pytest.mark.parametrize("field", ["commit", "vendor", "malformed"])
def test_lock_mismatch_refuses_build(settings, field):
    settings = combination(settings)
    path = settings.workspace / "kapa_knowledge" / "deps.lock"
    lock = json.loads(path.read_text())
    if field == "vendor":
        lock["kapa_skills"]["vendor"]["commit"] = "b" * 40
    elif field == "commit":
        lock["kapa_skills"]["commit"] = "b" * 40
    else:
        lock = []
    path.write_text(json.dumps(lock))
    commit(path.parent)
    collected(settings)
    with pytest.raises(p.PipelineError, match="workspace_dependency_mismatch"):
        p.build(settings, release_id="r1")


@pytest.mark.parametrize("change", ["head", "dirty", "missing"])
def test_activation_refuses_changed_workspace_without_pointer(settings, change):
    settings = combination(settings)
    collected(settings)
    p.build(settings, release_id="r1")
    api = FakeHfApi()
    p.publish(settings, release_id="r1", api=api)
    path = settings.workspace / "kapa_skills"
    if change == "missing":
        path.rename(path.with_name("missing-skills"))
    else:
        (path / "README").write_text("new revision")
        if change == "head":
            commit(path)  # The old pinned object still exists: not sufficient.
    with pytest.raises(p.PipelineError) as error:
        p.activate(settings, release_id="r1", api=api)
    assert error.value.error_code == "workspace_revision_mismatch"
    assert not settings.active_manifest.exists()


def test_rollback_refuses_changed_workspace_preserves_pointer(settings):
    settings = combination(settings)
    apis = {}
    for release in ("r1", "r2"):
        collected(settings, release)
        p.build(settings, release_id=release)
        apis[release] = FakeHfApi()
        p.publish(settings, release_id=release, api=apis[release])
        p.activate(settings, release_id=release, api=apis[release])
    old = settings.active_manifest.read_bytes()
    path = settings.workspace / "kapa_skills"
    (path / "README").write_text("changed")
    commit(path)
    with pytest.raises(p.PipelineError, match="workspace_revision_mismatch"):
        p.rollback(settings, api=apis["r1"])
    assert settings.active_manifest.read_bytes() == old


def test_legacy_manifest_without_required_pins_refuses(settings):
    settings = combination(settings)
    with pytest.raises(p.PipelineError, match="workspace_revision_mismatch"):
        p._verify_workspace(settings, {"github_revisions": {"pacappkor/kapa_runtime": "a" * 40}})


def test_dirty_optional_sibling_also_refuses(settings):
    settings = combination(settings)
    optional = repo(settings.workspace, "kapa_mcp")
    (optional / "untracked").write_text("fixture")
    with pytest.raises(w.WorkspaceError, match="workspace_repository_dirty"):
        w.capture(settings)


def test_present_non_git_sibling_is_not_treated_as_missing(settings):
    settings = combination(settings)
    (settings.workspace / "kapa_mcp").mkdir()
    with pytest.raises(w.WorkspaceError, match="workspace_revision_unavailable"):
        w.capture(settings)


def test_git_environment_is_minimal_and_read_only(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_DIR", "untrusted")
    monkeypatch.setenv("GIT_ASKPASS", "untrusted")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "1")
    monkeypatch.setenv("HF_TOKEN", "TEST-not-a-real-secret")
    def run(command, env, max_bytes):
        kwargs = {"env": env}
        assert max_bytes is None
        assert command[1:3] == ["-c", "safe.directory=" + tmp_path.resolve().as_posix()]
        assert "GIT_DIR" not in kwargs["env"]
        assert "HF_TOKEN" not in kwargs["env"]
        assert kwargs["env"]["GIT_OPTIONAL_LOCKS"] == "0"
        assert kwargs["env"]["GIT_CONFIG_NOSYSTEM"] == "1"
        assert kwargs["env"]["GIT_ATTR_NOSYSTEM"] == "1"
        assert kwargs["env"]["GIT_CONFIG_GLOBAL"] == w.os.devnull
        assert "core.fsmonitor=false" in command
        assert "core.hooksPath=" in command
        assert "credential.helper=" in command
        assert "protocol.allow=never" in command
        assert kwargs["env"]["GIT_NO_LAZY_FETCH"] == "1"
        assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
        assert kwargs["env"]["GIT_ASKPASS"] == ""
        assert command[-2:] == ["rev-parse", "HEAD"]
        return "a" * 40
    monkeypatch.setattr(w, "_bounded_git_output", run)
    assert w._run_git(tmp_path, "rev-parse", "HEAD") == "a" * 40


@pytest.mark.parametrize("options", [
    {"required_repos": ["../escape"]}, {"required_repos": "kapa_skills"},
    {"required_repos": ["kapa_skills", "kapa_skills"]},
    {"sibling_repos": [], "required_repos": ["kapa_skills"]},
    {"sibling_repos": ["unknown"]},
])
def test_invalid_repository_config(tmp_path, options):
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps({"workspace": ".", **options}))
    with pytest.raises(ConfigurationError):
        RuntimeSettings.load(path)


def test_repository_config_defaults_and_override(tmp_path):
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps({"workspace": "."}))
    assert RuntimeSettings.load(path).required_repos == ("kapa_knowledge", "kapa_skills")
    path.write_text(json.dumps({"workspace": ".", "sibling_repos": ["kapa_skills"],
                               "required_repos": ["kapa_skills"]}))
    assert RuntimeSettings.load(path).sibling_repos == ("kapa_skills",)


def test_clean_repository_passes(tmp_path):
    path = repo(tmp_path, "normal")
    assert w.revision(path) == git(path, "rev-parse", "HEAD")


def test_untrusted_owner_refused_before_git(tmp_path, monkeypatch):
    path = repo(tmp_path, "untrusted")
    monkeypatch.setattr(w, "owner_ids", lambda _: frozenset({"sandbox"}))
    monkeypatch.setattr(w, "current_owner_ids", lambda: frozenset({"operator"}))
    monkeypatch.setattr(w, "_run_git", lambda *a: pytest.fail("Git ran before ownership check"))
    with pytest.raises(w.WorkspaceError, match="workspace_repository_untrusted_owner"):
        w.revision(path)


def test_explicit_trusted_owner_passes(tmp_path, monkeypatch):
    path = repo(tmp_path, "trusted")
    monkeypatch.setattr(w, "owner_ids", lambda _: frozenset({"sandbox"}))
    monkeypatch.setattr(w, "current_owner_ids", lambda: frozenset({"operator"}))
    assert w.revision(path, ("SANDBOX",)) == git(path, "rev-parse", "HEAD")


@pytest.mark.parametrize("key,value", [
    ("core.fsmonitor", "run-me"), ("include.path", "nonexistent"),
    ("includeIf.gitdir:*.path", "nonexistent"), ("core.hooksPath", "hooks"),
    ("diff.test.textconv", "run-me"), ("diff.test.command", "run-me"),
    ("merge.test.driver", "run-me"), ("filter.test.clean", "run-me"),
    ("gpg.program", "run-me"), ("custom.test.command", "run-me"),
    ("core.alternateRefsCommand", "run-me"), ("custom.test.cmd", "run-me"),
    ("protocol.ext.allow", "always"), ("protocol.allow", "never"),
    ("extensions.partialClone", "origin"), ("remote.origin.promisor", "true"),
    ("core.bare", "true"),
])
def test_executable_config_refused(tmp_path, key, value):
    path = repo(tmp_path, "unsafe")
    git(path, "config", key, value)
    with pytest.raises(w.WorkspaceError, match="workspace_repository_unsafe_config"):
        w.revision(path)


@pytest.mark.parametrize("key,value", [
    ("credential.helper", "run-me"), ("credential.test.command", "run-me"),
    ("credential.test.cmd", "run-me"),
    ("core.sshCommand", "run-me"), ("core.askPass", "run-me"),
    ("protocol.version", "2"),
    ("uploadpack.packObjectsHook", "run-me"), ("receivepack.test", "run-me"),
])
def test_network_only_config_accepted(tmp_path, key, value):
    path = repo(tmp_path, "network-config")
    git(path, "config", key, value)
    assert w.revision(path) == git(path, "rev-parse", "HEAD")


def test_credential_helper_never_executes(tmp_path):
    path = repo(tmp_path, "credential")
    marker = tmp_path / "executed"
    git(path, "config", "credential.helper", "!echo executed > '" + marker.as_posix() + "'")
    assert w.revision(path) == git(path, "rev-parse", "HEAD")
    assert not marker.exists()


def test_filter_command_never_executes(tmp_path):
    path = repo(tmp_path, "filter")
    marker = tmp_path / "executed"
    (path / ".gitattributes").write_text("README filter=evil\n")
    git(path, "config", "filter.evil.clean", "echo executed > '" + marker.as_posix() + "'")
    (path / "README").write_text("trigger clean filter\n")
    with pytest.raises(w.WorkspaceError, match="workspace_repository_unsafe_config"):
        w.revision(path)
    assert not marker.exists()


@pytest.mark.parametrize("location", ["working", "index", "info"])
def test_filter_attributes_refused_without_config(tmp_path, location):
    path = repo(tmp_path, "attributes")
    attr = path / (".git/info/attributes" if location == "info" else ".gitattributes")
    attr.write_text("README filter=evil\n")
    if location == "index":
        git(path, "add", ".gitattributes")
        attr.unlink()
    with pytest.raises(w.WorkspaceError, match="workspace_repository_unsafe_config"):
        w.revision(path)


def test_fsmonitor_command_never_executes(tmp_path):
    path = repo(tmp_path, "fsmonitor")
    marker = tmp_path / "executed"
    git(path, "config", "core.fsmonitor", "echo executed > '" + marker.as_posix() + "'")
    with pytest.raises(w.WorkspaceError, match="workspace_repository_unsafe_config"):
        w.revision(path)
    assert not marker.exists()


def test_worktree_config_include_refused(tmp_path):
    path = repo(tmp_path, "worktree-config")
    git(path, "config", "extensions.worktreeConfig", "true")
    git(path, "config", "--worktree", "include.path", "nonexistent")
    with pytest.raises(w.WorkspaceError, match="workspace_repository_unsafe_config"):
        w.revision(path)


def test_git_metadata_owner_also_requires_trust(tmp_path, monkeypatch):
    path = repo(tmp_path, "metadata")
    monkeypatch.setattr(w, "current_owner_ids", lambda: frozenset({"operator"}))
    monkeypatch.setattr(w, "owner_ids", lambda p: frozenset({"sandbox" if p.name == "config" else "operator"}))
    with pytest.raises(w.WorkspaceError, match="workspace_repository_untrusted_owner"):
        w.revision(path)
    assert w.revision(path, ("sandbox",)) == git(path, "rev-parse", "HEAD")


def test_false_fsmonitor_is_safe(tmp_path):
    path = repo(tmp_path, "normal")
    git(path, "config", "core.fsmonitor", "false")
    assert w.revision(path) == git(path, "rev-parse", "HEAD")


@pytest.mark.parametrize("owners", ["sandbox", [1], [""], ["a\x00b"]])
def test_invalid_trusted_owner_config(tmp_path, owners):
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps({"workspace": ".", "trusted_repo_owners": owners}))
    with pytest.raises(ConfigurationError):
        RuntimeSettings.load(path)


def test_trusted_owner_config_loads(tmp_path):
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps({"workspace": ".", "trusted_repo_owners": ["S-1-5-21-example"]}))
    assert RuntimeSettings.load(path).trusted_repo_owners == ("S-1-5-21-example",)


@pytest.mark.parametrize("admin", [False, True])
@pytest.mark.parametrize("owner", ["user-sid", "s-1-5-32-544", "other-sid"])
def test_windows_owner_semantics(tmp_path, monkeypatch, admin, owner):
    from kapa_runtime.releases import owners
    path = repo(tmp_path, "ownership")
    monkeypatch.setattr(owners, "_windows_user_ids", lambda: frozenset({"user-sid"}))
    monkeypatch.setattr(owners, "_windows_is_admin", lambda: admin)
    monkeypatch.setattr(w, "current_owner_ids", owners._windows_current_owner_ids)
    monkeypatch.setattr(w, "owner_ids", lambda _: frozenset({owner}))
    if owner == "user-sid" or owner == owners.ADMINISTRATORS_SID and admin:
        assert w.revision(path) == git(path, "rev-parse", "HEAD")
    else:
        with pytest.raises(w.WorkspaceError, match="workspace_repository_untrusted_owner"):
            w.revision(path)


def test_redirected_worktree_refused(tmp_path):
    path = repo(tmp_path, "redirected")
    other = tmp_path / "other"
    other.mkdir()
    (other / "README").write_bytes((path / "README").read_bytes())
    (path / "untracked").write_text("must not be hidden")
    git(path, "config", "core.worktree", str(other))
    with pytest.raises(w.WorkspaceError, match="workspace_repository_unsafe_config"):
        w.revision(path)


def symlink(link, target, directory=False):
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError:
        pytest.skip("symlink creation unavailable")


@pytest.mark.parametrize("location", ["working", "info"])
def test_attribute_symlink_refused(tmp_path, location):
    path = repo(tmp_path, "attributes-link")
    target = tmp_path / "external"
    target.write_text("innocent\n")
    attr = path / (".gitattributes" if location == "working" else ".git/info/attributes")
    symlink(attr, target)
    with pytest.raises(w.WorkspaceError, match="workspace_repository_unsafe_config"):
        w.revision(path)


def test_attribute_fifo_refused(tmp_path):
    import os
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO unavailable")
    path = repo(tmp_path, "fifo")
    os.mkfifo(path / ".gitattributes")
    with pytest.raises(w.WorkspaceError, match="workspace_repository_unsafe_config"):
        w.revision(path)


def test_oversized_attributes_refused(tmp_path, monkeypatch):
    from kapa_runtime.storage import metadata
    path = repo(tmp_path, "large")
    monkeypatch.setattr(metadata, "MAX_METADATA_BYTES", 16)
    (path / ".gitattributes").write_text("x" * 17)
    with pytest.raises(w.WorkspaceError, match="workspace_repository_unsafe_config"):
        w.revision(path)


def test_external_git_dir_refused(tmp_path):
    path = repo(tmp_path, "external-git")
    external = tmp_path / "metadata"
    (path / ".git").rename(external)
    (path / ".git").write_text("gitdir: " + external.as_posix())
    with pytest.raises(w.WorkspaceError, match="workspace_repository_unsafe_config"):
        w.revision(path)


def test_registered_linked_worktree_passes(tmp_path):
    path = repo(tmp_path, "main")
    linked = tmp_path / "linked"
    git(path, "worktree", "add", "--detach", str(linked))
    assert w.revision(linked) == git(path, "rev-parse", "HEAD")


def test_relative_linked_worktree_backlink_passes(tmp_path, monkeypatch):
    path = repo(tmp_path, "main-relative")
    linked = tmp_path / "linked-relative"
    git(path, "worktree", "add", "--detach", str(linked))
    git_dir = Path(git(linked, "rev-parse", "--absolute-git-dir"))
    (git_dir / "gitdir").write_text(
        os.path.relpath(linked / ".git", git_dir) + "\n", encoding="utf-8")
    # The backlink must not depend on the runtime process's working directory.
    monkeypatch.chdir(tmp_path)
    assert w.revision(linked) == git(path, "rev-parse", "HEAD")


def test_fake_promisor_refused_before_object_reads(tmp_path, monkeypatch):
    path = repo(tmp_path, "promisor")
    git(path, "config", "remote.origin.promisor", "true")
    git(path, "config", "remote.origin.url", "ext::must-not-execute")
    original = w._run_git
    def checked(repo_path, *args):
        assert args[0] == "config", "promisor reached an object-reading command"
        return original(repo_path, *args)
    monkeypatch.setattr(w, "_run_git", checked)
    with pytest.raises(w.WorkspaceError, match="workspace_repository_unsafe_config"):
        w.revision(path)


def test_attribute_directory_refused(tmp_path):
    path = repo(tmp_path, "attribute-dir")
    (path / ".gitattributes").mkdir()
    with pytest.raises(w.WorkspaceError, match="workspace_repository_unsafe_config"):
        w.revision(path)


@pytest.mark.parametrize("linked", [False, True])
def test_git_marker_refused_without_read(tmp_path, monkeypatch, linked):
    path = tmp_path / "marker"
    path.mkdir()
    target = tmp_path / "external"
    target.write_text("gitdir: must-not-read\n")
    marker = path / ".git"
    if linked:
        symlink(marker, target)
    else:
        marker.write_text("gitdir: must-not-read\n")
    monkeypatch.setattr(w, "current_owner_ids", lambda: frozenset({"operator"}))
    monkeypatch.setattr(w, "owner_ids", lambda _: frozenset({"operator" if linked else "sandbox"}))
    monkeypatch.setattr(w, "read_metadata", lambda *a: pytest.fail("marker read before trust"))
    monkeypatch.setattr(w, "_run_git", lambda *a: pytest.fail("Git ran before trust"))
    with pytest.raises(w.WorkspaceError, match="workspace_repository_untrusted_owner"):
        w.revision(path)


def test_git_marker_reparse_refused_before_read(tmp_path, monkeypatch):
    path = tmp_path / "reparse-marker"
    path.mkdir()
    marker = path / ".git"
    marker.write_text("gitdir: external\n")
    original = w._is_link
    monkeypatch.setattr(w, "_is_link", lambda p: p == marker or original(p))
    monkeypatch.setattr(w, "read_metadata", lambda *a: pytest.fail("reparse marker read"))
    with pytest.raises(w.WorkspaceError, match="workspace_repository_untrusted_owner"):
        w.revision(path)


def test_oversized_git_marker_uses_metadata_limit(tmp_path, monkeypatch):
    from kapa_runtime.storage import metadata
    path = tmp_path / "large-marker"
    path.mkdir()
    (path / ".git").write_text("gitdir: " + "x" * 17)
    monkeypatch.setattr(metadata, "MAX_METADATA_BYTES", 16)
    with pytest.raises(w.WorkspaceError, match="workspace_repository_untrusted_owner"):
        w.revision(path)


@pytest.mark.parametrize("working", ["deleted", "small"])
def test_oversized_index_attributes_refused_before_show(tmp_path, monkeypatch, working):
    from kapa_runtime.storage import metadata
    path = repo(tmp_path, "large-index")
    attr = path / ".gitattributes"
    attr.write_text("#" + "x" * 32)
    git(path, "add", ".gitattributes")
    if working == "deleted":
        attr.unlink()
    else:
        attr.write_text("# small\n")
    monkeypatch.setattr(metadata, "MAX_METADATA_BYTES", 16)
    original = w._run_git
    def checked(repo_path, *args, **kwargs):
        assert args[0] not in {"show", "status"}, "oversized blob reached capture/status"
        return original(repo_path, *args, **kwargs)
    monkeypatch.setattr(w, "_run_git", checked)
    with pytest.raises(w.WorkspaceError, match="workspace_repository_unsafe_config"):
        w.revision(path)


@pytest.mark.parametrize("content", [" " * 10, "#" + " " * 9])
@pytest.mark.parametrize("budget", [39, 40])
def test_index_attribute_raw_bytes_charged_before_show(tmp_path, monkeypatch, content, budget):
    path = repo(tmp_path, "raw-index")
    for name in ("a", "b"):
        child = path / name
        child.mkdir()
        (child / ".gitattributes").write_bytes(content.encode("utf-8"))
    head = commit(path)
    # Working files consume 20 bytes, and each indexed raw blob consumes 10.
    monkeypatch.setattr(w, "MAX_ATTRIBUTE_SCAN_BYTES", budget)
    original = w._run_git
    shown = []
    def checked(repo_path, *args, **kwargs):
        if args[0] == "show":
            shown.append(args[1])
            assert len(shown) <= (1 if budget < 40 else 2)
        if budget < 40:
            assert args[0] != "status", "over-budget attributes reached status"
        return original(repo_path, *args, **kwargs)
    monkeypatch.setattr(w, "_run_git", checked)
    if budget < 40:
        with pytest.raises(w.WorkspaceError, match="workspace_repository_unsafe_config"):
            w.revision(path)
        assert shown == [":a/.gitattributes"]
    else:
        assert w.revision(path) == head
        assert shown == [":a/.gitattributes", ":b/.gitattributes"]


def test_git_blob_output_capture_is_bounded(tmp_path):
    path = repo(tmp_path, "bounded-output")
    with pytest.raises(w.WorkspaceError, match="workspace_repository_unsafe_config"):
        w._run_git(path, "show", "HEAD:README", max_bytes=3)
    size = int(git(path, "cat-file", "-s", "HEAD:README"))
    assert w._run_git(path, "show", "HEAD:README", max_bytes=size) == "fixture"


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_hidden_index_changes_refused(tmp_path, flag):
    path = repo(tmp_path, "flags")
    git(path, "update-index", flag, "README")
    (path / "README").write_text("changed\n")
    assert not git(path, "status", "--porcelain")
    with pytest.raises(w.WorkspaceError, match="workspace_repository_dirty"):
        w.revision(path)


@pytest.mark.parametrize("key", ["core.attributesFile", "core.excludesFile"])
def test_external_attributes_and_excludes_refused(tmp_path, key):
    path = repo(tmp_path, "external-config")
    git(path, "config", key, str(tmp_path / "external"))
    with pytest.raises(w.WorkspaceError, match="workspace_repository_unsafe_config"):
        w.revision(path)


@pytest.mark.parametrize("name", ["config", "config.worktree"])
def test_root_config_is_not_metadata(tmp_path, monkeypatch, name):
    path = repo(tmp_path, "root-config")
    (path / name).write_text("ordinary input\n")
    commit(path)
    monkeypatch.setattr(w, "current_owner_ids", lambda: frozenset({"operator"}))
    monkeypatch.setattr(w, "owner_ids", lambda p: frozenset({"other" if p == path / name else "operator"}))
    assert w.revision(path) == git(path, "rev-parse", "HEAD")


def test_broken_optional_link_refused(settings):
    symlink(settings.workspace / "kapa_mcp", settings.workspace / "missing", directory=True)
    with pytest.raises(w.WorkspaceError, match="workspace_revision_unavailable"):
        w.capture(settings)


@pytest.mark.parametrize("kind", ["gitlink", "gitmodules", "indexed-gitmodules", "modules", "linked-modules"])
def test_submodules_rejected_before_attributes(tmp_path, monkeypatch, kind):
    path = repo(tmp_path, "unsupported")
    if kind == "gitlink":
        git(path, "update-index", "--add", "--cacheinfo",
            "160000," + git(path, "rev-parse", "HEAD") + ",child")
        # No inspection, traversal or Git process may enter this child.
        (path / "child").mkdir()
    elif kind == "gitmodules" or kind == "indexed-gitmodules":
        (path / ".gitmodules").write_text("must not be read")
        if kind == "indexed-gitmodules":
            git(path, "add", ".gitmodules")
            (path / ".gitmodules").unlink()
    else:
        (path / ".git" / "modules").mkdir()
        if kind == "linked-modules":
            linked = tmp_path / "linked-unsupported"
            git(path, "worktree", "add", "--detach", str(linked))
            path = linked
    monkeypatch.setattr(w.os, "walk", lambda *a, **kw: pytest.fail("attribute walk before rejection"))
    original = w._run_git
    def checked(repo_path, *args, **kwargs):
        assert repo_path.resolve() == path.resolve()
        assert args[0] not in {"status", "show", "cat-file"}
        return original(repo_path, *args, **kwargs)
    monkeypatch.setattr(w, "_run_git", checked)
    with pytest.raises(w.WorkspaceError, match="workspace_repository_submodules_unsupported"):
        w.revision(path)


@pytest.mark.parametrize("kind", ["entries", "bytes"])
def test_attribute_scan_aggregate_budget(tmp_path, monkeypatch, kind):
    path = repo(tmp_path, "aggregate")
    for name in ("a", "b", "c"):
        child = path / name
        child.mkdir()
        (child / ".gitattributes").write_text("# harmless")
    monkeypatch.setattr(w, "MAX_ATTRIBUTE_SCAN_ENTRIES" if kind == "entries"
                        else "MAX_ATTRIBUTE_SCAN_BYTES", 8 if kind == "entries" else 15)
    with pytest.raises(w.WorkspaceError, match="workspace_repository_unsafe_config"):
        w.revision(path)


def test_parent_alias_uses_canonical_path(tmp_path, monkeypatch):
    path = repo(tmp_path, "canonical")
    alias = tmp_path / "alias"
    symlink(alias, tmp_path, directory=True)
    spelling = alias / path.name
    original = w._bounded_git_output
    def checked(command, env, max_bytes=None):
        assert command[2] == "safe.directory=" + path.resolve().as_posix()
        assert command[command.index("-C") + 1] == str(path.resolve())
        return original(command, env, max_bytes)
    monkeypatch.setattr(w, "_bounded_git_output", checked)
    assert w.revision(spelling) == git(path, "rev-parse", "HEAD")


def test_ignored_files_remain_excluded(tmp_path):
    path = repo(tmp_path, "ignored")
    (path / ".gitignore").write_text("generated\n")
    commit(path)
    (path / "generated").write_text("operator-local\n")
    assert w.revision(path) == git(path, "rev-parse", "HEAD")


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_output_limit_kills_before_process_finishes(tmp_path, stream):
    import os
    import sys
    import time
    started = time.monotonic()
    with pytest.raises(w.WorkspaceError, match="workspace_repository_unsafe_config"):
        w._bounded_git_output([sys.executable, "-I", "-c",
            f"import sys,time; sys.{stream}.write('x'*100000); sys.{stream}.flush(); time.sleep(60)"],
            os.environ.copy(), 128)
    assert time.monotonic() - started < 10


@pytest.mark.parametrize("kind", ["config", "status"])
def test_all_git_output_uses_default_limit(tmp_path, monkeypatch, kind):
    path = repo(tmp_path, "large-output")
    if kind == "config":
        git(path, "config", "custom.payload", "x" * 2000)
        args = ("config", "--local", "--list")
    else:
        for i in range(40):
            (path / (str(i) + "x" * 40)).write_text("local")
        args = ("status", "--porcelain", "--untracked-files=all")
    monkeypatch.setattr(w, "MAX_GIT_OUTPUT_BYTES", 512)
    with pytest.raises(w.WorkspaceError, match="workspace_repository_unsafe_config"):
        w._run_git(path, *args)


@pytest.mark.parametrize("entry", ["repo", "marker"])
@pytest.mark.parametrize("operation", ["capture", "revision", "git", "source"])
def test_reparse_rejected_before_target_following(settings, monkeypatch, entry, operation):
    from types import SimpleNamespace
    import stat
    path = settings.workspace / "kapa_mcp"
    path.mkdir()
    marker = path / ".git"
    marker.mkdir()
    target = path if entry == "repo" else marker
    original_lstat = Path.lstat
    def lstat(self):
        if self == target:
            return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
        if entry == "repo" and self.is_relative_to(path):
            pytest.fail("child probed before checkout reparse gate")
        return original_lstat(self)
    def forbid(original):
        def checked(self, *args, **kwargs):
            if (self == path or self.is_relative_to(path)) and kwargs.get("follow_symlinks", True):
                pytest.fail("target-following operation before reparse gate")
            return original(self, *args, **kwargs)
        return checked
    with monkeypatch.context() as patch:
        patch.setattr(Path, "lstat", lstat)
        for name in ("exists", "is_file", "is_dir", "resolve", "stat"):
            patch.setattr(Path, name, forbid(getattr(Path, name)))
        patch.setattr(w, "owner_ids", lambda *a: pytest.fail("owner lookup before reparse gate"))
        patch.setattr(w, "_run_git", lambda *a: pytest.fail("Git before reparse gate"))
        patch.setattr(p, "__file__", str(path / "src" / "kapa_runtime" / "pipeline.py"))
        with pytest.raises((w.WorkspaceError, p.PipelineError)):
            if operation == "capture":
                w.capture(replace(settings, sibling_repos=("kapa_mcp",), required_repos=()))
            elif operation == "revision":
                w.revision(path)
            elif operation == "git":
                w.git(path, "status")
            else:
                SOURCE_GIT_REVISION(settings)


def test_unreadable_sibling_entry_not_optional(settings, monkeypatch):
    from pathlib import Path
    path = settings.workspace / "kapa_mcp"
    original = Path.lstat
    def lstat(self):
        if self == path:
            raise PermissionError()
        return original(self)
    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(w.WorkspaceError, match="workspace_revision_unavailable"):
        w.capture(replace(settings, sibling_repos=("kapa_mcp",), required_repos=()))


@pytest.mark.parametrize("failure", ["timeout", "overflow"])
def test_process_group_terminates_descendant(tmp_path, monkeypatch, failure):
    import os
    import sys
    import time
    pidfile = tmp_path / "child.pid"
    child = "import time; time.sleep(60)"
    parent = ("import subprocess,sys,time,pathlib; "
              f"p=subprocess.Popen([sys.executable,'-I','-c',{child!r}]); "
              f"pathlib.Path({str(pidfile)!r}).write_text(str(p.pid)); "
              + ("sys.stdout.write('x'*100000); sys.stdout.flush(); "
                 if failure == "overflow" else "") + "time.sleep(60)")
    monkeypatch.setattr(w, "GIT_TIMEOUT_SECONDS", 2)
    started = time.monotonic()
    error = subprocess.TimeoutExpired if failure == "timeout" else w.WorkspaceError
    with pytest.raises(error):
        w._bounded_git_output([sys.executable, "-I", "-c", parent], os.environ.copy(), 128)
    assert time.monotonic() - started < 10
    pid = int(pidfile.read_text())
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        handle = kernel.OpenProcess(0x1000, False, pid)
        if handle:
            code = wintypes.DWORD()
            kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            try:
                assert kernel.GetExitCodeProcess(handle, ctypes.byref(code))
                assert code.value != 259  # STILL_ACTIVE
            finally:
                kernel.CloseHandle(handle)
    else:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            pass
        else:
            # A killed orphan may remain a zombie until init reaps it.
            from pathlib import Path
            assert ") Z " in Path(f"/proc/{pid}/stat").read_text()


@pytest.mark.parametrize("unsupported", [False, True])
def test_runtime_source_uses_guard_and_configured_owner(tmp_path, monkeypatch, unsupported):
    from types import SimpleNamespace
    path = repo(tmp_path, "runtime-source")
    if unsupported:
        git(path, "update-index", "--add", "--cacheinfo",
            "160000," + git(path, "rev-parse", "HEAD") + ",child")
    monkeypatch.setattr(p, "__file__", str(path / "src" / "kapa_runtime" / "pipeline.py"))
    monkeypatch.setattr(w, "current_owner_ids", lambda: frozenset({"worker"}))
    monkeypatch.setattr(w, "owner_ids", lambda _: frozenset({"trusted"}))
    settings = SimpleNamespace(trusted_repo_owners=("trusted",))
    if unsupported:
        with pytest.raises(p.PipelineError, match="workspace_repository_submodules_unsupported"):
            p._git_revision(settings)
    else:
        assert p._git_revision(settings) == git(path, "rev-parse", "HEAD")
