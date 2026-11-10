# SPDX-License-Identifier: GPL-3.0-only
"""Secret loading and sanitization; no implicit credential discovery."""
from .loader import CredentialError, Secret, diagnose, load_secret, redact

__all__ = ["CredentialError", "Secret", "diagnose", "load_secret", "redact"]
