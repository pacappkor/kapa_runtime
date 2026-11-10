# SPDX-License-Identifier: GPL-3.0-only
"""Allowlisted external data adapters."""
from .molit_rtms import CollectionResult, SanitizedPage, collect

__all__ = ["CollectionResult", "SanitizedPage", "collect"]
