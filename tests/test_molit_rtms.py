# SPDX-License-Identifier: GPL-3.0-only
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, quote_plus

import httpx
import pytest

from kapa_runtime.adapters.molit_rtms import CollectionError, collect

FIXTURES = Path(__file__).parent / "fixtures" / "rtms"
KEY = "TESTKEY-not-real+/=&"


@dataclass
class Spec:
    host: str = "apis.data.go.kr"
    path: str = "/1613000/RTMSDataSvcAptTrade/getRTMSDataSvcAptTrade"
    method: str = "GET"
    service_id: str = "molit-apartment-sale"
    dataset_id: str = "15126469"
    provider: str = "MOLIT"
    page_size_max: int = 1000


class FakeSecret:
    def reveal(self):
        return KEY


def xml(name):
    return (FIXTURES / name).read_bytes()


def run(responses, **kwargs):
    calls = []
    waits = []
    def handler(request):
        calls.append(request)
        response = responses[min(len(calls) - 1, len(responses) - 1)]
        if isinstance(response, Exception):
            raise response
        if isinstance(response, bytes):
            return httpx.Response(200, content=response)
        return response
    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        result = collect(Spec(), FakeSecret(), lawd_code="11110", deal_ymd="202510",
                         run_id="test-run", client=client, page_size=1, sleep=waits.append, **kwargs)
    return result, calls, waits


def test_success_multi_page():
    result, calls, _ = run([xml("sale-page-1.xml"), xml("sale-page-2.xml")])
    assert result.observation.status == "success"
    assert result.observation.pages_expected == result.observation.pages_received == 2
    assert result.observation.count == 2
    assert [r.url.params["pageNo"] for r in calls] == ["1", "2"]
    assert calls[0].url.params["serviceKey"] == KEY  # encoded exactly once on wire
    assert calls[0].extensions["timeout"]["connect"] == 10


def test_zero():
    result, _, _ = run([xml("zero.xml")])
    assert result.observation.status == "zero"
    assert result.observation.count == 0
    assert result.pages[0].items == []


@pytest.mark.parametrize("name", ["auth-error.xml", "gateway-error.xml"])
def test_http_200_auth_error_not_retried(name):
    result, calls, waits = run([xml(name)])
    assert result.observation.status == "failure"
    assert result.observation.warnings == ("authentication_error",)
    assert len(calls) == 1 and waits == []


@pytest.mark.parametrize("second,warning", [
    (xml("auth-error.xml"), "authentication_error"),
    (xml("sale-page-1.xml"), "missing_or_duplicate_page"),
    (xml("sale-page-2.xml").replace(b"<pageNo>2", b"<pageNo>3"), "missing_or_duplicate_page"),
    (xml("sale-page-2.xml").replace(b"<totalCount>2", b"<totalCount>3"), "total_count_drift"),
])
def test_partial(second, warning):
    result, _, _ = run([xml("sale-page-1.xml"), second])
    assert result.observation.status == "partial"
    assert result.observation.pages_received == 1
    assert result.observation.warnings == (warning,)


def test_page_budget():
    result, calls, _ = run([xml("sale-page-1.xml")], max_pages=1)
    assert result.observation.status == "partial"
    assert len(calls) == 1


def test_retry_after_then_success():
    result, calls, waits = run([httpx.Response(429, headers={"Retry-After": "2"}), xml("sale.xml")])
    assert result.observation.status == "success"
    assert len(calls) == 2 and waits == [2]


def test_5xx_exhaustion():
    result, calls, waits = run([httpx.Response(503)])
    assert result.observation.status == "failure"
    assert result.observation.warnings == ("retry_exhausted",)
    assert len(calls) == 3 and len(waits) == 2


def test_timeout_then_success():
    result, calls, _ = run([httpx.ReadTimeout(KEY), xml("sale.xml")])
    assert result.observation.status == "success" and len(calls) == 2


@pytest.mark.parametrize("status,warning", [(302, "redirect_refused"), (401, "authentication_error"), (403, "authentication_error"), (400, "http_error")])
def test_http_failure(status, warning):
    result, calls, _ = run([httpx.Response(status, headers={"Location": "https://example.invalid/"})])
    assert len(calls) == 1
    assert result.observation.warnings == (warning,)


def test_oversized_response_and_cumulative_budget():
    result, _, _ = run([xml("sale.xml")], max_bytes=20)
    assert result.observation.warnings == ("response_too_large",)
    first = xml("sale-page-1.xml")
    result, _, _ = run([first, xml("sale-page-2.xml")], max_bytes=len(first) + 50)
    assert result.observation.status == "partial"


def test_keys_never_persist_or_escape():
    from xml.sax.saxutils import escape
    echo = " ".join([KEY, quote(KEY, safe=""), quote_plus(KEY)])
    payload = xml("sale.xml").replace(b"<aptDong>909", ("<aptDong>" + escape(echo)).encode())
    result, _, _ = run([payload])
    for value in [KEY, quote(KEY, safe=""), quote_plus(KEY)]:
        assert value not in repr(result)
        assert value.encode() not in result.pages[0].sanitized
    result, _, _ = run([httpx.ConnectError(echo)])
    assert result.observation.warnings == ("transport_error",)
    for value in [KEY, quote(KEY, safe=""), quote_plus(KEY)]:
        assert value not in repr(result.observation.request) + repr(result)
    with httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("network"))) as client:
        with pytest.raises(CollectionError) as error:
            collect(Spec(host=KEY), FakeSecret(), lawd_code="11110", deal_ymd="202510", run_id="x", client=client)
        assert KEY not in str(error.value)


@pytest.mark.parametrize("payload", [b"broken", b"<!DOCTYPE response [<!ENTITY x 'x'>]><response>&x;</response>", b"<response><header><resultCode>000</resultCode></header></response>"])
def test_invalid_xml_not_zero(payload):
    result, _, _ = run([payload])
    assert result.observation.status == "failure"
    assert result.observation.count is None


def test_identical_items_on_distinct_numbered_pages():
    first = xml('sale-page-1.xml')
    result, calls, _ = run([first, first.replace(b'<pageNo>1', b'<pageNo>2')])
    assert result.observation.status == 'success'
    assert result.observation.count == 2
    assert result.pages[0].items == result.pages[1].items
    assert len(calls) == 2
