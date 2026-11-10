# SPDX-License-Identifier: GPL-3.0-only
"""Packaged service allowlist, not an arbitrary endpoint registry."""
from __future__ import annotations

from dataclasses import dataclass, field
from importlib.resources import files
import json


@dataclass(frozen=True)
class ServiceSpec:
    service_id: str
    dataset_id: str
    provider: str
    priority: str
    credential_alias: str | None
    host: str | None = None
    path: str | None = None
    method: str | None = None
    operation: str | None = None
    required_params: tuple[str, ...] = ()
    page_size_max: int | None = None
    approval_status: str = "approval-unverified"
    operation_verified: bool = False
    last_connection_check: str | None = None
    quota: dict | None = None
    storage_and_redistribution_reviewed: bool = False
    normalized_extra_fields: tuple[str, ...] = ()
    param_patterns: dict[str, str] = field(default_factory=dict)


class ServiceRegistry:
    def __init__(self, services: list[ServiceSpec]):
        self.services = {spec.service_id: spec for spec in services}
        if len(self.services) != len(services):
            raise ValueError("duplicate service identifier")

    @classmethod
    def load(cls) -> ServiceRegistry:
        raw = json.loads(files(__package__).joinpath("services.json").read_text(encoding="utf-8"))
        services = []
        for item in raw["services"]:
            item = dict(item)
            item["required_params"] = tuple(item.get("required_params", ()))
            item["normalized_extra_fields"] = tuple(item.get("normalized_extra_fields", ()))
            services.append(ServiceSpec(**item))
        return cls(services)

    def get(self, service_id: str) -> ServiceSpec:
        return self.services[service_id]
