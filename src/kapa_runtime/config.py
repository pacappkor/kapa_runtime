# SPDX-License-Identifier: GPL-3.0-only
"""Explicit, key-free runtime configuration. No filesystem writes on load."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path


class ConfigurationError(ValueError):
    """Invalid runtime configuration (without echoing input values)."""


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConfigurationError("duplicate configuration field")
        result[key] = value
    return result


@dataclass(frozen=True)
class RuntimeSettings:
    workspace: Path
    data_root: Path
    credential_files: dict[str, Path] = field(default_factory=dict)
    hf_namespace: str | None = None
    hf_repos: dict[str, str] = field(default_factory=dict)
    hf_visibilities: dict[str, str] = field(default_factory=dict)
    hf_staging_names: dict[str, str] = field(default_factory=dict)
    sibling_repos: tuple[str, ...] = ("kapa_edu_1", "kapa_edu_2", "kapa_skills", "kapa_knowledge", "kapa_mcp")
    required_repos: tuple[str, ...] = ("kapa_knowledge", "kapa_skills")

    trusted_repo_owners: tuple[str, ...] = ()

    def __post_init__(self):
        if (not isinstance(self.trusted_repo_owners, (list, tuple))
                or any(not isinstance(owner, str) or not owner.strip() or "\x00" in owner
                       for owner in self.trusted_repo_owners)):
            raise ConfigurationError("invalid trusted repository owners")
        object.__setattr__(self, "trusted_repo_owners", tuple(self.trusted_repo_owners))
        allowed = {"kapa_edu_1", "kapa_edu_2", "kapa_skills", "kapa_knowledge", "kapa_mcp"}
        for names in (self.sibling_repos, self.required_repos):
            if (not isinstance(names, (list, tuple))
                    or any(not isinstance(name, str) or name not in allowed for name in names)
                    or len(names) != len(set(names))):
                raise ConfigurationError("invalid workspace repository list")
        if not set(self.required_repos) <= set(self.sibling_repos):
            raise ConfigurationError("required repositories must be configured siblings")
        object.__setattr__(self, "sibling_repos", tuple(self.sibling_repos))
        object.__setattr__(self, "required_repos", tuple(self.required_repos))

    @classmethod
    def load(cls, path: Path | None = None) -> RuntimeSettings:
        selected = path if path is not None else os.environ.get("KAPA_RUNTIME_CONFIG")
        if not selected:
            raise ConfigurationError("explicit config path or KAPA_RUNTIME_CONFIG required")
        try:
            config_path = Path(selected).absolute()
            raw = json.loads(config_path.read_text(encoding="utf-8-sig"),
                             object_pairs_hook=_unique,
                             parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            base = config_path.parent

            def resolve(value):
                if not isinstance(value, str) or not value or "\x00" in value:
                    raise ValueError()
                p = Path(value)
                return Path(os.path.abspath(base / p if not p.is_absolute() else p))

            workspace = resolve(raw.get("workspace", raw.get("workspace_root")))
            data = raw.get("data_root", raw.get("paths", {}).get("data_root"))
            data_root = resolve(data) if data is not None else workspace / "_kapa_data"
            credentials = raw.get("credential_files")
            if credentials is None:
                credentials = {k: v["fallback_file"] for k, v in raw.get("credentials", {}).items()
                               if "fallback_file" in v}
            if not isinstance(credentials, dict) or set(credentials) - {
                "data_go_kr", "hugging_face", "hugging_face_write", "hugging_face_read", "rone", "vworld"
            }:
                raise ValueError()
            files = {k: resolve(v) for k, v in credentials.items()}
            hf = raw.get("hugging_face", {})
            namespace = raw.get("hf_namespace", hf.get("namespace"))
            repos = raw.get("hf_repos", hf.get("repositories", {}))
            if namespace is not None and (not isinstance(namespace, str) or not namespace.strip()):
                raise ValueError()
            if not isinstance(repos, dict):
                raise ValueError()
            names, visibilities, staging_names = {}, {}, {}
            for role, entry in repos.items():
                if isinstance(entry, str):
                    name, visibility = entry, "private"
                elif isinstance(entry, dict) and set(entry) <= {"name", "visibility", "staging_name"}:
                    name, visibility = entry.get("name"), entry.get("visibility", "private")
                else:
                    raise ValueError()
                if (not isinstance(role, str) or not isinstance(name, str) or not name
                        or visibility not in ("private", "public") or visibility == "public" and role != "market"):
                    raise ValueError()
                staging = entry.get("staging_name", name + "-staging") if isinstance(entry, dict) else name + "-staging"
                if (not isinstance(staging, str) or not staging or staging == name
                        or not all(c.isascii() and (c.isalnum() or c in "_.-") for c in staging)
                        or not staging[0].isascii() or not staging[0].isalnum()):
                    raise ValueError()
                if isinstance(entry, dict) and "staging_name" in entry and role != "market":
                    raise ValueError()
                staging_names[role] = staging
                names[role], visibilities[role] = name, visibility
            for candidate in (names.get("market"), staging_names.get("market")):
                if candidate is not None and (candidate.casefold() in {
                    "kapa_knowledge", "kapa_mcp", "kapa_skills", "kapa_runtime"
                } or candidate.casefold().startswith("kapa_edu_")):
                    raise ValueError()
            configured = [name.casefold() for name in names.values()]
            staged = [name.casefold() for name in staging_names.values()]
            if (len(set(configured)) != len(configured) or set(configured).intersection(staged)
                    or len(set(staged)) != len(staged)):
                raise ValueError()
            return cls(workspace, data_root, files, namespace, names, visibilities, staging_names,
                       raw.get("sibling_repos", cls.__dataclass_fields__["sibling_repos"].default),
                       raw.get("required_repos", cls.__dataclass_fields__["required_repos"].default),
                       raw.get("trusted_repo_owners", ()))
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            raise ConfigurationError("unable to load valid runtime configuration") from None

    @property
    def staging_dir(self) -> Path:
        return self.data_root / "staging"

    @property
    def snapshots_dir(self) -> Path:
        return self.data_root / "snapshots"

    @property
    def cache_dir(self) -> Path:
        return self.data_root / "cache"

    @property
    def state_db(self) -> Path:
        return self.data_root / "state" / "runtime.sqlite3"

    @property
    def manifests_dir(self) -> Path:
        return self.data_root / "manifests"

    @property
    def active_manifest(self) -> Path:
        return self.manifests_dir / "active.json"
