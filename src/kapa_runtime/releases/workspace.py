# SPDX-License-Identifier: GPL-3.0-only
"""Offline, read-only Git provenance and sibling compatibility checks."""
from __future__ import annotations

import os
from pathlib import Path
import re
import signal
import subprocess
import stat
import threading

from ..storage import metadata
from ..storage.metadata import read_json, read_metadata
from .owners import current_owner_ids, owner_ids


MAX_GIT_OUTPUT_BYTES = 4 * 1024 * 1024
MAX_ATTRIBUTE_SCAN_ENTRIES = 100_000
MAX_ATTRIBUTE_SCAN_BYTES = 4 * 1024 * 1024
GIT_TIMEOUT_SECONDS = 30


class WorkspaceError(ValueError):
    """Fixed error codes; never expose Git diagnostics or local paths."""


def _trusted_owner(repo: Path, trusted_repo_owners: tuple[str, ...]) -> None:
    try:
        trusted = current_owner_ids() | {value.casefold() for value in trusted_repo_owners}
        # A separate git directory/config can have a different owner from the tree.
        marker = repo / ".git"
        marker_info = marker.lstat()
        if _is_link(marker):
            raise OSError()
        paths = [repo, marker]
        # Establish checkout/marker trust without reading the gitdir pointer.
        if any(not owner_ids(path).intersection(trusted) for path in paths):
            raise OSError()
        if stat.S_ISREG(marker_info.st_mode):
            value = read_metadata(marker).strip()
            if not value.startswith("gitdir: "):
                raise OSError()
            git_dir = repo / value[8:]
            if _is_link(git_dir):
                raise OSError()
            if not owner_ids(git_dir).intersection(trusted):
                raise OSError()
            paths.append(git_dir)
            common = git_dir / "commondir"
            if _metadata_present(common):
                common_dir = git_dir / read_metadata(common).strip()
                if (_is_link(common_dir)
                        or not owner_ids(common_dir).intersection(trusted)):
                    raise OSError()
                paths.append(common_dir)
        elif not stat.S_ISDIR(marker_info.st_mode):
            raise OSError()
        # Only Git metadata directories hold config; root-level names are inputs.
        metadata_dirs = [path for path in paths
                         if path != repo and stat.S_ISDIR(path.lstat().st_mode)]
        for path in metadata_dirs:
            for filename in ("config", "config.worktree"):
                if _metadata_present(path / filename):
                    paths.append(path / filename)
        if any(not owner_ids(path).intersection(trusted) for path in paths):
            raise OSError()
    except (OSError, ValueError, UnicodeError):
        raise WorkspaceError("workspace_repository_untrusted_owner") from None


def _run_git(repo: Path, *args: str, max_bytes: int | None = None) -> str:
    repo = repo.resolve()
    # Drop inherited Git routing/config and credentials. Suppress optional index
    # writes, global config, external fsmonitor and replacement-object semantics.
    env = {key: value for key, value in os.environ.items()
           if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_OPTIONAL_LOCKS="0", GIT_NO_REPLACE_OBJECTS="1",
               GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="", GIT_ATTR_NOSYSTEM="1",
               GIT_NO_LAZY_FETCH="1")
    try:
        command = ["git", "-c", "safe.directory=" + repo.as_posix(),
                   "-c", "core.fsmonitor=false", "-c", "core.hooksPath=",
                   "-c", "credential.helper=", "-c", "protocol.allow=never",
                   "-C", str(repo), *args]
        return _bounded_git_output(command, env, max_bytes)

    except (OSError, subprocess.SubprocessError):
        raise WorkspaceError("workspace_revision_unavailable") from None


def _bounded_git_output(command: list[str], env: dict[str, str],
                        max_bytes: int | None = None) -> str:
    """Drain both pipes with a shared byte budget, killing before buffering excess."""
    limit = MAX_GIT_OUTPUT_BYTES if max_bytes is None else max_bytes
    payload = bytearray()
    exceeded = threading.Event()
    lock = threading.Lock()
    total = 0
    group_options = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
                     if os.name == "nt" else {"start_new_session": True})
    with subprocess.Popen(command, env=env, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, **group_options) as process:
        def kill_group():
            try:
                if os.name == "nt":
                    # CREATE_NEW_PROCESS_GROUP alone does not make kill() recursive.
                    subprocess.run([str(Path(env["SYSTEMROOT"]) / "System32" / "taskkill.exe"),
                                    "/PID", str(process.pid), "/T", "/F"],
                                   env=env, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=5, check=False)
                else:
                    os.killpg(process.pid, signal.SIGKILL)
            except (OSError, subprocess.SubprocessError):
                pass
            finally:
                if process.poll() is None:
                    process.kill()
        def drain(pipe, keep):
            nonlocal total
            try:
                while chunk := pipe.read1(min(8192, limit + 1)):
                    with lock:
                        total += len(chunk)
                        if total > limit:
                            exceeded.set()
                            kill_group()
                            return
                        if keep:
                            payload.extend(chunk)
            finally:
                pipe.close()
        readers = [threading.Thread(target=drain, args=(pipe, keep), daemon=True)
                   for pipe, keep in ((process.stdout, True), (process.stderr, False))]
        for reader in readers:
            reader.start()
        try:
            process.wait(timeout=GIT_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            kill_group()
            process.wait()
            raise
        finally:
            for reader in readers:
                reader.join(timeout=1)
        if exceeded.is_set():
            raise WorkspaceError("workspace_repository_unsafe_config")
        if any(reader.is_alive() for reader in readers) or process.returncode:
            raise WorkspaceError("workspace_revision_unavailable")
    return payload.decode("utf-8", errors="surrogateescape").strip()


def _metadata_present(path: Path) -> bool:
    """Probe metadata without contacting a symlink/reparse target."""
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    if _is_link(path):
        raise OSError()
    return True


def _is_link(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _bound_git_dir(repo: Path) -> Path:
    """Bind Git's tree and metadata to the inspected checkout before object reads."""
    expected = repo.resolve()
    if Path(_run_git(repo, "rev-parse", "--show-toplevel")).resolve() != expected:
        raise WorkspaceError("workspace_repository_unsafe_config")
    git_dir = Path(_run_git(repo, "rev-parse", "--absolute-git-dir")).resolve()
    if git_dir.is_relative_to(expected):
        return git_dir
    # A linked worktree has a common repository, a registered worktrees entry,
    # and a backlink to this exact checkout's .git marker.
    try:
        common = (git_dir / read_metadata(git_dir / "commondir").strip()).resolve()
        backlink = (git_dir / read_metadata(git_dir / "gitdir").strip()).resolve()
        if (git_dir.parent != common / "worktrees"
                or backlink != expected / ".git"
                or not (common / "config").is_file()):
            raise ValueError()
    except (OSError, ValueError, UnicodeError):
        raise WorkspaceError("workspace_repository_unsafe_config") from None
    return git_dir


def _inspect(repo: Path, trusted_repo_owners: tuple[str, ...]) -> Path:
    # Check the checkout before even probing its marker: a child lstat would
    # already traverse a reparse checkout (including a remote target).
    try:
        info = repo.lstat()
        if _is_link(repo):
            raise WorkspaceError("workspace_repository_unsafe_config")
        if not stat.S_ISDIR(info.st_mode):
            raise WorkspaceError("workspace_revision_unavailable")
        (repo / ".git").lstat()
    except OSError:
        raise WorkspaceError("workspace_revision_unavailable") from None
    _trusted_owner(repo, trusted_repo_owners)
    repo = repo.resolve()
    # Never resolve includes: even a conditional include is refused before status.
    config = _run_git(repo, "config", "--local", "--no-includes", "--list", "--null")
    # Without the extension Git can reject --worktree on linked checkouts.
    if any(entry.partition("\n")[0].lower() == "extensions.worktreeconfig"
           and entry.partition("\n")[2].lower() not in {"false", "no", "off", "0"}
           for entry in config.split("\x00")):
        config += "\x00" + _run_git(repo, "config", "--worktree", "--no-includes", "--list", "--null")
    for entry in config.split("\x00"):
        key, _, value = entry.partition("\n")
        key = key.lower()
        # Reject transport opt-ins as well as local execution hooks: object
        # reads in partial clones can otherwise trigger demand fetching.
        if (key.startswith(("include.", "includeif.", "filter.", "gpg."))
                or key in {"core.worktree", "core.hookspath", "core.alternaterefscommand",
                           "core.attributesfile", "core.excludesfile", "extensions.partialclone"}
                or key == "core.bare" and value.lower() not in {"false", "no", "off", "0"}
                or key.startswith("remote.") and key.endswith(".promisor")
                   and value.lower() not in {"false", "no", "off", "0"}
                or key.startswith("protocol.") and key.endswith(".allow")
                or key.endswith((".command", ".cmd")) and not key.startswith("credential.")
                or key.startswith("diff.") and key.endswith(".textconv")
                or key.startswith("merge.") and key.endswith(".driver")
                or key == "core.fsmonitor" and value.lower() not in {"false", "no", "off", "0"}):
            raise WorkspaceError("workspace_repository_unsafe_config")
    # Default-deny submodules before any attribute traversal or recursive status.
    index = _run_git(repo, "ls-files", "-s", "-z").split("\x00")
    if any(entry.startswith("160000 ") or entry.partition("\t")[2] == ".gitmodules"
           for entry in index) or os.path.lexists(repo / ".gitmodules"):
        raise WorkspaceError("workspace_repository_submodules_unsupported")
    git_dir = _bound_git_dir(repo)
    common_marker = git_dir / "commondir"
    common = ((git_dir / read_metadata(common_marker).strip()).resolve()
              if common_marker.exists() else git_dir)
    if any(os.path.lexists(path / "modules") for path in (git_dir, common)):
        raise WorkspaceError("workspace_repository_submodules_unsupported")
    if any(entry and (entry[0].islower() or entry[0] == "S")
           for entry in _run_git(repo, "ls-files", "-v", "-z").split("\x00")):
        raise WorkspaceError("workspace_repository_dirty")
    try:
        attribute_bytes = 0
        scan_entries = 0
        def attribute(text):
            nonlocal attribute_bytes
            attribute_bytes += len(text.encode("utf-8", errors="surrogateescape"))
            if attribute_bytes > MAX_ATTRIBUTE_SCAN_BYTES or "filter=" in text:
                raise WorkspaceError("workspace_repository_unsafe_config")
        def unreadable(error):
            raise error
        for root, dirs, files in os.walk(repo, followlinks=False, onerror=unreadable):
            scan_entries += 1 + len(dirs) + len(files)
            if scan_entries > MAX_ATTRIBUTE_SCAN_ENTRIES:
                raise WorkspaceError("workspace_repository_unsafe_config")
            dirs[:] = [name for name in dirs if name != ".git" and not _is_link(Path(root) / name)]
            attr = Path(root) / ".gitattributes"
            if attr.exists() or attr.is_symlink():
                attribute(read_metadata(attr))
        info = Path(_run_git(repo, "rev-parse", "--git-path", "info/attributes"))
        if not info.is_absolute():
            info = repo / info
        if info.exists() or info.is_symlink():
            attribute(read_metadata(info))
        # Index blobs must be checked too, including deleted/modified working files.
        for entry in index:
            header, _, name = entry.partition("\t")
            if name and Path(name).name == ".gitattributes":
                mode, oid, stage = header.split()
                if stage != "0" or not re.fullmatch(r"[0-9a-f]{40,64}", oid):
                    raise ValueError()
                size = int(_run_git(repo, "cat-file", "-s", oid))
                if not 0 <= size <= metadata.MAX_METADATA_BYTES:
                    raise ValueError()
                # Charge the raw blob size before reading; Git output is stripped.
                attribute_bytes += size
                if attribute_bytes > MAX_ATTRIBUTE_SCAN_BYTES:
                    raise WorkspaceError("workspace_repository_unsafe_config")
                text = _run_git(repo, "show", ":" + name,
                                max_bytes=metadata.MAX_METADATA_BYTES)
                if "filter=" in text:
                    raise WorkspaceError("workspace_repository_unsafe_config")
    except WorkspaceError:
        raise
    except (OSError, ValueError, UnicodeError):
        raise WorkspaceError("workspace_repository_unsafe_config") from None
    return repo


def git(repo: Path, *args: str, trusted_repo_owners: tuple[str, ...] = ()) -> str:
    """Only run Git after ownership/config/attribute checks, including source Git."""
    repo = _inspect(repo, trusted_repo_owners)
    return _run_git(repo, *args)


def revision(repo: Path, trusted_repo_owners: tuple[str, ...] = ()) -> str:
    # _inspect checks the checkout and its own marker without following links;
    # never probe a child before rejecting a redirected checkout.
    repo = _inspect(repo, trusted_repo_owners)
    if _run_git(repo, "status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=none"):
        raise WorkspaceError("workspace_repository_dirty")
    sha = _run_git(repo, "rev-parse", "--verify", "HEAD^{commit}")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise WorkspaceError("workspace_revision_unavailable")
    return sha


def compatibility(settings, revisions: dict) -> None:
    knowledge = revisions.get("pacappkor/kapa_knowledge")
    if knowledge is None:
        return
    try:
        lock = read_json(settings.workspace / "kapa_knowledge" / "deps.lock")
        skills = lock["kapa_skills"]
        head = revisions.get("pacappkor/kapa_skills")
        if head is None or skills["commit"] != head or skills["vendor"]["commit"] != head:
            raise ValueError()
    except (OSError, ValueError, KeyError, TypeError):
        raise WorkspaceError("workspace_dependency_mismatch") from None


def capture(settings) -> tuple[dict, list[dict]]:
    revisions, warnings = {}, []
    for name in settings.sibling_repos:
        repo = settings.workspace / name
        key = "pacappkor/" + name
        try:
            repo.lstat()
        except FileNotFoundError:
            present = False
        except OSError:
            raise WorkspaceError("workspace_revision_unavailable") from None
        else:
            if _is_link(repo):
                raise WorkspaceError("workspace_revision_unavailable")
            present = True
        if not present:
            revisions[key] = None
            warnings.append({"code": "workspace_repository_unavailable", "repo": key})
            if name in settings.required_repos:
                raise WorkspaceError("workspace_required_repository_unavailable")
        else:
            revisions[key] = revision(repo, settings.trusted_repo_owners)
    compatibility(settings, revisions)
    return revisions, warnings


def verify(settings, manifest: dict) -> None:
    """Require operator checkout of the pinned combination; never fetch/checkout."""
    revisions = manifest["github_revisions"]
    try:
        for name in settings.required_repos:
            pinned = revisions.get("pacappkor/" + name)
            if (not isinstance(pinned, str) or not re.fullmatch(r"[0-9a-f]{40}", pinned)
                    or revision(settings.workspace / name, settings.trusted_repo_owners) != pinned):
                raise WorkspaceError("workspace_revision_mismatch")
        # When knowledge participates, verify its lock against the recorded pair.
        # Only read the current lock when knowledge itself was verified above.
        if "kapa_knowledge" in settings.required_repos:
            compatibility(settings, revisions)
    except WorkspaceError:
        raise WorkspaceError("workspace_revision_mismatch") from None
