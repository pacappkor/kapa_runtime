# SPDX-License-Identifier: GPL-3.0-only
"""Release contracts; HF publishing dependencies load only when requested."""
from .activate import ActivationError, activate, rollback
from .manifest import (
    ManifestError, build_manifest, dump_manifest, dumps_manifest,
    load_manifest, loads_manifest, manifest_digest, validate_manifest,
)

__all__ = [
    "ActivationError", "ManifestError", "PublishError", "VerificationReport",
    "activate", "rollback", "build_manifest", "dump_manifest", "dumps_manifest",
    "load_manifest", "loads_manifest", "manifest_digest", "validate_manifest",
]


def __getattr__(name):
    if name in {"PublishError", "VerificationReport"}:
        from . import hf
        return getattr(hf, name)
    raise AttributeError(name)
