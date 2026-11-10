# SPDX-License-Identifier: GPL-3.0-only
"""Single bounded, checked regular-file boundary for metadata and JSON reads."""
from pathlib import Path
import json
import os
import stat

from .parquet import _safe_path

MAX_METADATA_BYTES = 16 * 1024 * 1024


def read_metadata(path: Path, *, as_bytes=False):
    path = Path(os.path.abspath(path))
    _safe_path(path)
    checked = os.lstat(path)
    if not stat.S_ISREG(checked.st_mode) or checked.st_size > MAX_METADATA_BYTES:
        raise ValueError("metadata requires a bounded regular file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as stream:
        opened = os.fstat(stream.fileno())
        _safe_path(path)
        current = os.lstat(path)
        identity = (opened.st_dev, opened.st_ino)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_size > MAX_METADATA_BYTES
                or identity != (checked.st_dev, checked.st_ino)
                or identity != (current.st_dev, current.st_ino)):
            raise ValueError("metadata file changed during open")
        payload = stream.read(MAX_METADATA_BYTES + 1)
        _safe_path(path)
        current = os.lstat(path)
        if len(payload) > MAX_METADATA_BYTES or identity != (current.st_dev, current.st_ino):
            raise ValueError("metadata file changed during read")
    return payload if as_bytes else payload.decode("utf-8")


def read_json(path: Path):
    return json.loads(read_metadata(path))
