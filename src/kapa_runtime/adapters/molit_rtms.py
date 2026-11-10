# SPDX-License-Identifier: GPL-3.0-only
"""Bounded, key-free observations from the two allowlisted RTMS operations."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import math
import random
import re
import time
from typing import Callable
from uuid import uuid4
from xml.etree.ElementTree import ParseError, tostring

from defusedxml.ElementTree import fromstring
from defusedxml.common import DefusedXmlException
import httpx

from kapa_runtime.credentials import redact
from kapa_runtime.models import Observation, ObservationStatus

PARSER_VERSION = "molit-rtms/1"
_ENDPOINTS = {
    "molit-apartment-sale": "/1613000/RTMSDataSvcAptTrade/getRTMSDataSvcAptTrade",
    "molit-apartment-rent": "/1613000/RTMSDataSvcAptRent/getRTMSDataSvcAptRent",
}


class CollectionError(ValueError):
    """Safe fixed error code; never carries a request, URL or upstream message."""


class AuthenticationError(CollectionError):
    pass


class ProviderError(CollectionError):
    pass


class ResponseValidationError(CollectionError):
    pass


class ResponseTooLarge(CollectionError):
    pass


class RetryExhausted(CollectionError):
    pass


@dataclass(frozen=True)
class SanitizedPage:
    page_no: int
    sha256: str
    sanitized: bytes
    items: list[dict[str, str | None]]


@dataclass(frozen=True)
class CollectionResult:
    observation: Observation
    pages: list[SanitizedPage]


def _integer(text: str | None) -> int:
    if text is None or re.fullmatch(r"[0-9]+", text.strip()) is None:
        raise ResponseValidationError("invalid_pagination")
    return int(text)


def _parse(payload: bytes, secret) -> tuple[SanitizedPage, int, int]:
    try:
        root = fromstring(payload, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except (ParseError, DefusedXmlException, ValueError):
        raise ResponseValidationError("invalid_xml") from None
    if root.tag == "OpenAPI_ServiceResponse":
        code = root.findtext("cmmMsgHeader/returnReasonCode", "")
        message = root.findtext("cmmMsgHeader/errMsg", "") + root.findtext("cmmMsgHeader/returnAuthMsg", "")
    elif root.tag == "response":
        code = root.findtext("header/resultCode", "").strip()
        message = root.findtext("header/resultMsg", "")
    else:
        raise ResponseValidationError("invalid_envelope")
    if code != "000":
        if code in {"20", "21", "22", "30", "31", "32"} or any(
            token in (code + message).upper() for token in ("SERVICE_KEY", "ACCESS_DENIED", "UNAUTHORIZED")
        ):
            raise AuthenticationError("authentication_error")
        raise ProviderError("provider_error")
    body = root.find("body")
    if body is None:
        raise ResponseValidationError("missing_body")
    total = _integer(body.findtext("totalCount"))
    number = _integer(body.findtext("pageNo"))
    size = _integer(body.findtext("numOfRows"))
    if number < 1 or size < 1:
        raise ResponseValidationError("invalid_pagination")
    # Redact decoded XML nodes as well as the final serialized envelope: a key
    # may have been XML-escaped by the upstream service.
    for node in root.iter():
        node.tag = redact(node.tag, [secret])
        if node.text:
            node.text = redact(node.text, [secret])
        if node.tail:
            node.tail = redact(node.tail, [secret])
        node.attrib = {redact(k, [secret]): redact(v, [secret]) for k, v in node.attrib.items()}
    items = []
    for item in body.findall("items/item"):
        row = {}
        for field in item:
            if field.tag in row or len(field):
                raise ResponseValidationError("invalid_item_schema")
            row[field.tag] = field.text  # blanks preserved until normalization
        items.append(row)
    sanitized = redact(tostring(root, encoding="unicode"), [secret]).encode("utf-8")
    return SanitizedPage(number, hashlib.sha256(sanitized).hexdigest(), sanitized, items), total, size


def _retry_delay(value: str | None, attempt: int) -> float:
    if value:
        try:
            seconds = float(value)
        except ValueError:
            try:
                seconds = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
            except (ValueError, TypeError, OverflowError):
                seconds = -1
        if math.isfinite(seconds) and seconds >= 0:
            # A provider wait exceeding our execution budget is not shortened.
            if seconds > 60:
                raise RetryExhausted("retry_after_exceeds_budget")
            return seconds
    return min(2 ** attempt + random.uniform(0, 0.25), 8)


def _fetch(client, url, params, budget, sleep, before_request):
    for attempt in range(3):
        retry_after = None
        transient = False
        before_request()
        try:
            with client.stream("GET", url, params=params, follow_redirects=False,
                               timeout=httpx.Timeout(30, connect=10)) as response:
                if response.status_code in {401, 403}:
                    raise AuthenticationError("authentication_error")
                if 300 <= response.status_code < 400:
                    raise ProviderError("redirect_refused")
                if response.status_code == 429 or 500 <= response.status_code < 600:
                    transient = True
                    retry_after = response.headers.get("Retry-After")
                elif response.status_code != 200:
                    raise ProviderError("http_error")
                else:
                    chunks = []
                    for chunk in response.iter_bytes():
                        budget[0] -= len(chunk)
                        if budget[0] < 0:
                            raise ResponseTooLarge("response_too_large")
                        chunks.append(chunk)
                    return b"".join(chunks)
        except httpx.TimeoutException:
            transient = True
        except httpx.HTTPError:
            # Do not retain exception text/context: httpx includes secret URLs.
            raise ProviderError("transport_error") from None
        if transient:
            if attempt == 2:
                raise RetryExhausted("retry_exhausted")
            sleep(_retry_delay(retry_after, attempt))
    raise RetryExhausted("retry_exhausted")


def collect(spec, secret, *, lawd_code: str, deal_ymd: str, client: httpx.Client,
            run_id: str, page_size: int = 1000, max_pages: int = 50,
            max_bytes: int = 5_000_000, sleep: Callable[[float], None] = time.sleep,
            before_request: Callable[[], None] = lambda: None) -> CollectionResult:
    """Collect one region/month. Operational failures become failure/partial.

    Invalid local arguments raise CollectionError. At most 3 attempts per page,
    50 pages and a cumulative decompressed-byte budget; no error body is kept.
    Caller-owned clients must not install secret-logging event hooks.
    """
    if (spec.host != "apis.data.go.kr" or spec.path != _ENDPOINTS.get(spec.service_id)
            or spec.method != "GET" or spec.service_id not in _ENDPOINTS):
        raise CollectionError("endpoint_not_allowed")
    if not isinstance(lawd_code, str) or re.fullmatch(r"[0-9]{5}", lawd_code) is None:
        raise CollectionError("invalid_lawd_code")
    if not isinstance(deal_ymd, str) or re.fullmatch(r"[0-9]{6}", deal_ymd) is None:
        raise CollectionError("invalid_deal_ymd")
    try:
        datetime.strptime(deal_ymd, "%Y%m")
    except ValueError:
        raise CollectionError("invalid_deal_ymd") from None
    if (type(page_size) is not int or not 1 <= page_size <= min(1000, spec.page_size_max)
            or type(max_pages) is not int or not 1 <= max_pages <= 50
            or type(max_bytes) is not int or not 1 <= max_bytes <= 5_000_000):
        raise CollectionError("invalid_collection_budget")
    started = datetime.now(timezone.utc)
    request = {"LAWD_CD": lawd_code, "DEAL_YMD": deal_ymd, "numOfRows": str(page_size)}
    pages = []
    total = expected = None
    warnings = []
    budget = [max_bytes]
    try:
        for page_no in range(1, max_pages + 1):
            payload = _fetch(client, f"https://{spec.host}{spec.path}",
                             {**request, "pageNo": str(page_no), "serviceKey": secret.reveal()},
                             budget, sleep, before_request)
            page, reported_total, size = _parse(payload, secret)
            if page.page_no != page_no:
                raise ResponseValidationError("missing_or_duplicate_page")
            if size != page_size:
                raise ResponseValidationError("page_size_mismatch")
            if total is None:
                total = reported_total
                expected = max(1, (total + page_size - 1) // page_size)
            elif total != reported_total:
                raise ResponseValidationError("total_count_drift")
            if len(page.items) != min(page_size, max(0, total - (page_no - 1) * page_size)):
                raise ResponseValidationError("page_row_count_mismatch")
            pages.append(page)
            if page_no == expected:
                break
        if len(pages) != expected:
            raise ResponseValidationError("page_budget_exhausted")
    except CollectionError as exc:
        warnings.append(redact(str(exc), [secret]))
    if warnings:
        status = ObservationStatus.PARTIAL if pages else ObservationStatus.FAILURE
    else:
        status = ObservationStatus.ZERO if total == 0 else ObservationStatus.SUCCESS
    observation = Observation(
        observation_id=str(uuid4()), run_id=redact(run_id, [secret]),
        provider=redact(spec.provider, [secret]), service_id=spec.service_id,
        dataset_id=redact(spec.dataset_id, [secret]), endpoint_id=spec.service_id,
        request={k: redact(v, [secret]) for k, v in request.items()},
        started_at=started, fetched_at=datetime.now(timezone.utc), status=status,
        pages_expected=expected, pages_received=len(pages), count=total,
        response_schema="molit-rtms-xml/1", parser_version=PARSER_VERSION,
        sanitized_payload_sha256=hashlib.sha256(b"".join(p.sanitized for p in pages)).hexdigest(),
        warnings=tuple(warnings), coverage={
            "lawd_code": redact(lawd_code, [secret]), "deal_ymd": redact(deal_ymd, [secret]),
            "complete": not warnings, "rows_received": sum(len(p.items) for p in pages),
            "historical_availability": "unknown",
        },
    )
    return CollectionResult(observation, pages)
