# SPDX-License-Identifier: GPL-3.0-only
"""The sole boundary permitted to read explicitly configured credential files."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
import os
from pathlib import Path
import re
import stat
from urllib.parse import quote, quote_plus, unquote

from kapa_runtime.config import RuntimeSettings

ENV_NAMES = {
    "data_go_kr": "DATA_GO_KR_SERVICE_KEY",
    "hugging_face": "HF_TOKEN",
    "hugging_face_write": "HF_TOKEN_WRITE",
    "hugging_face_read": "HF_TOKEN_READ",
    "rone": "RONE_API_KEY",
    "vworld": "VWORLD_API_KEY",
}


class CredentialError(ValueError):
    """Only a static error code is retained, never unsafe exception text."""

    def __init__(self, code: str):
        self.error_code = code
        super().__init__(code)


class Secret:
    __slots__ = ("__value",)

    def __init__(self, value: str):
        self.__value = value

    def __repr__(self) -> str:
        return "Secret(***)"

    __str__ = __repr__

    def reveal(self) -> str:
        return self.__value


def _parse(value: str) -> tuple[str | None, str | None]:
    value = value.removeprefix("﻿").rstrip("\r\n")
    if any((c.isspace() and c not in "\r\n") or (not c.isprintable() and c not in "\r\n") for c in value):
        return None, "invalid_whitespace"
    lines = [line for line in value.splitlines() if line]
    if not lines:
        return None, "empty"
    if len(lines) != 1:
        return None, "multiple_lines"
    value = lines[0]
    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", value):
        return None, "label_form"
    if any(c.isspace() or not c.isprintable() for c in value):
        return None, "invalid_whitespace"
    return value, None


def _read_file(path: Path) -> tuple[str | None, str | None]:
    # Check every existing path component: a safe-looking leaf under a junction
    # is still an indirect credential reference. Never resolve links first.
    error = None
    value = None
    try:
        absolute = Path(os.path.abspath(path))
        for part in (*reversed(absolute.parents), absolute):
            info = os.lstat(part)
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                return None, "unsafe_link"
        if not stat.S_ISREG(info.st_mode):
            return None, "not_regular_file"
        # O_NOFOLLOW closes the leaf symlink race on platforms that provide it.
        fd = os.open(absolute, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "r", encoding="utf-8-sig", newline="") as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                return None, "unsafe_link"
            value = stream.read()
    except FileNotFoundError:
        error = "missing_file"
    except (OSError, UnicodeError, ValueError):
        error = "file_read_error"
    return value, error


def _effective_alias(alias: str, settings: RuntimeSettings, env: Mapping[str, str]) -> str:
    # Defined-but-empty role credentials must fail closed. Only legacy fallback
    # is allowed: a new write token must never become the default read token.
    if (alias in ("hugging_face_write", "hugging_face_read")
            and ENV_NAMES[alias] not in env and alias not in settings.credential_files):
        return "hugging_face"
    return alias


def load_secret(alias: str, settings: RuntimeSettings, env: Mapping[str, str] = os.environ) -> Secret:
    if alias not in ENV_NAMES:
        raise CredentialError("unknown_alias")
    alias = _effective_alias(alias, settings, env)
    name = ENV_NAMES[alias]
    if name in env:
        value, error = _parse(env[name])
    elif alias in settings.credential_files:
        value, error = _read_file(settings.credential_files[alias])
        if error is None:
            value, error = _parse(value)
    else:
        value, error = None, "not_configured"
    if error:
        raise CredentialError(error)
    return Secret(value)


def diagnose(alias: str, settings: RuntimeSettings, env: Mapping[str, str] = os.environ) -> dict:
    effective = _effective_alias(alias, settings, env)
    name = ENV_NAMES.get(effective)
    source = "env" if name in env else "file" if effective in settings.credential_files else "missing"
    code = None
    try:
        load_secret(alias, settings, env)
    except CredentialError as error:
        code = error.error_code
    if code == "missing_file":
        source = "missing"
    result = {"alias": alias, "source": source, "status": "error" if code else "ok", "error_code": code}
    if alias == "hugging_face":
        result["usage"] = ["secret_scan"] + [
            role for role in ("hugging_face_write", "hugging_face_read")
            if _effective_alias(role, settings, env) == alias
        ]
    if alias in ("hugging_face_write", "hugging_face_read") and effective != alias and source != "missing":
        result["warning"] = ("legacy_hugging_face_used_for_read" if alias == "hugging_face_read"
                             else "legacy_hugging_face_used_for_write")
    return result


def redact(text: str, secrets: Iterable[Secret]) -> str:
    text = re.sub(r"%[0-9a-fA-F]{2}", lambda m: m[0].upper(), text)
    forms = set()
    for secret in secrets:
        value = secret.reveal()
        if value:
            forms.update((value, quote(value, safe=""), quote(value), quote_plus(value)))
    forms.update(unquote(form) for form in tuple(forms))
    forms = {re.sub(r"%[0-9a-fA-F]{2}", lambda m: m[0].upper(), form) for form in forms}
    for form in sorted(forms, key=len, reverse=True):
        text = text.replace(form, "***")
    decoded = unquote(text)
    if any(form in decoded for form in forms):
        for form in sorted(forms, key=len, reverse=True):
            decoded = decoded.replace(form, "***")
        text = decoded
    return text
