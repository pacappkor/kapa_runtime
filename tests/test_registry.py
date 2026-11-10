# SPDX-License-Identifier: GPL-3.0-only
import re

import pytest

from kapa_runtime.registry import ServiceRegistry


def test_all_services_and_unverified_boundaries():
    registry = ServiceRegistry.load()
    assert len(registry.services) == 17
    assert len({spec.dataset_id for spec in registry.services.values()}) == 17
    for spec in registry.services.values():
        assert not spec.storage_and_redistribution_reviewed
        if spec.credential_alias == "data_go_kr":
            assert spec.quota == {"daily_limit": 10000, "scope": "per-service",
                                  "timezone": "Asia/Seoul", "quota_source": "user-reported-2026-11-10"}
        else:
            assert spec.quota is None
        if spec.priority != "P0":
            assert spec.host is spec.path is None
            assert spec.approval_status == "approval-unverified"
            assert not spec.operation_verified
            assert spec.last_connection_check is None
    assert registry.get("land-use-plan-link").credential_alias is None
    with pytest.raises(KeyError):
        registry.get("https://arbitrary.invalid")


@pytest.mark.parametrize("kind,service,dataset", [("sale", "AptTrade", "15126469"), ("rent", "AptRent", "15126474")])
def test_verified_endpoints(kind, service, dataset):
    spec = ServiceRegistry.load().get(f"molit-apartment-{kind}")
    assert spec.dataset_id == dataset
    assert spec.host == "apis.data.go.kr"
    assert spec.path == f"/1613000/RTMSDataSvc{service}/getRTMSDataSvc{service}"
    assert spec.method == "GET"
    assert spec.operation == f"getRTMSDataSvc{service}"
    assert spec.required_params == ("LAWD_CD", "DEAL_YMD", "pageNo", "numOfRows")
    assert spec.page_size_max == 1000
    assert spec.approval_status == "connection-verified-2026-11-10"
    assert spec.operation_verified
    assert spec.last_connection_check == "2026-11-10"
    assert re.fullmatch(spec.param_patterns["LAWD_CD"], "11110")
    assert not re.fullmatch(spec.param_patterns["LAWD_CD"], "1111")
    assert re.fullmatch(spec.param_patterns["DEAL_YMD"], "202610")
    assert not re.fullmatch(spec.param_patterns["DEAL_YMD"], "202613")
