# SPDX-License-Identifier: GPL-3.0-only
"""Immutable local publication bytes; journal entries contain descriptors, not payloads."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile

from .hf import PublishError, _no_links, scan_secret_payload
from .manifest import relative_path


def inventory_digest(entries):
    return hashlib.sha256(json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class Spool:
    def __init__(self, root):
        self.root = Path(root).absolute()
        self.root.mkdir(parents=True, exist_ok=True)
        _no_links(self.root)

    def put(self, payload):
        digest = hashlib.sha256(payload).hexdigest()
        target = self.root / digest
        if not target.exists():
            fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=self.root)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                # Writers are serialized by StateStore; never replace a blob.
                os.link(temporary, target)
                if os.name != "nt":
                    fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
            finally:
                Path(temporary).unlink(missing_ok=True)
        self.read({"sha256": digest, "size": len(payload)})
        return {"sha256": digest, "size": len(payload)}

    def read(self, entry):
        digest, size = entry.get("sha256"), entry.get("size")
        if (not isinstance(digest, str) or not re.fullmatch("[0-9a-f]{64}", digest)
                or type(size) is not int or not 0 <= size <= 256 * 1024 * 1024):
            raise PublishError("Invalid spool descriptor")
        path = self.root / digest
        _no_links(path)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        with os.fdopen(os.open(path, flags), "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size != size:
                raise PublishError("Spool size mismatch")
            payload = stream.read(size + 1)
        if len(payload) != size or hashlib.sha256(payload).hexdigest() != digest:
            raise PublishError("Spool digest mismatch")
        return payload

    def freeze(self, payloads, *, schemas=None):
        return [{"path": relative_path(name), **self.put(payload),
                 "schema": (schemas or {}).get(name, "kapa-runtime-data/2")}
                for name, payload in sorted(payloads.items())]

    def replay(self, entries, digest, writer):
        if inventory_digest(entries) != digest:
            raise PublishError("Spool inventory digest mismatch")
        payloads = {}
        for entry in entries:
            if set(entry) != {"path", "sha256", "size", "schema"} or entry["path"] in payloads:
                raise PublishError("Invalid spool inventory")
            name = entry["path"]
            writer._scan_path(name)
            payload = self.read(entry)
            scan_secret_payload(payload, name, writer.secret_values(), schema_version=entry["schema"])
            payloads[name] = payload
        return payloads
