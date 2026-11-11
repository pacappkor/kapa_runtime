# SPDX-License-Identifier: GPL-3.0-only
"""Pure daily pair planning; estimates only, never a quota reservation or executor.

A coverage entry means both services were completely observed (including zero).
Dates passed as dates are already Asia/Seoul calendar dates; aware datetimes are
converted. Service starts are provisional, not assertions about API availability.
"""
from __future__ import annotations

import calendar
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import re
from zoneinfo import ZoneInfo

Pair = tuple[str, str]


def _month(value: str) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{6}", value):
        raise ValueError("month must be YYYYMM")
    year, month = int(value[:4]), int(value[4:])
    if not 1 <= year <= 9999 or not 1 <= month <= 12:
        raise ValueError("invalid month")
    return year * 12 + month - 1


def _ym(month: int) -> str:
    year, zero_month = divmod(month, 12)
    return f"{year:04d}{zero_month + 1:02d}"


def _day(value: date | datetime) -> date:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("datetime must be timezone-aware")
        return value.astimezone(ZoneInfo("Asia/Seoul")).date()
    if not isinstance(value, date):
        raise ValueError("expected calendar date")
    return value


@dataclass(frozen=True)
class ServiceRange:
    start: str
    end: str | None = None  # None means the current Seoul month, inclusive.


@dataclass(frozen=True)
class Budget:
    sale_requests: int = 9_000
    rent_requests: int = 9_000
    requests_per_pair: int = 150  # 50 pages, three attempts per page per service.
    refresh_percent: int = 20
    audit_percent: int = 5
    backfill_percent: int = 75
    batch_cap: int = 60

    def __post_init__(self) -> None:
        values = vars(self)
        if any(type(value) is not int or value < 0 for value in values.values()):
            raise ValueError("budget values must be nonnegative integers")
        if not 3 <= self.requests_per_pair <= 150 or self.requests_per_pair % 3:
            raise ValueError("pair reservation must be 3..150 in page units of three")
        if self.batch_cap > 60:
            raise ValueError("batch cap cannot exceed 60")
        if self.refresh_percent + self.audit_percent + self.backfill_percent != 100:
            raise ValueError("lane percentages must sum to 100")

    @property
    def pair_capacity(self) -> int:
        return min(self.batch_cap, self.sale_requests // self.requests_per_pair,
                   self.rent_requests // self.requests_per_pair)

    def lane_capacity(self, percent: int) -> int:
        return min(self.pair_capacity,
                   self.sale_requests * percent // (100 * self.requests_per_pair),
                   self.rent_requests * percent // (100 * self.requests_per_pair))


@dataclass(frozen=True)
class PlannedPair:
    lawd_code: str
    deal_ymd: str
    lane: str

    @property
    def pair(self) -> Pair:
        return self.lawd_code, self.deal_ymd


@dataclass(frozen=True)
class DailyPlan:
    day: date
    items: tuple[PlannedPair, ...]
    pair_capacity: int
    refresh_due: int
    refresh_share: int
    refresh_sla_shortfall: int
    reserved_requests_per_service: int
    overlap_start: str | None
    overlap_end: str | None


def refresh_due_on(ym: str, observed_at: date | datetime | None,
                   today: date | datetime) -> date | None:
    """Current month daily; prior 1..3 months weekly; prior 4..12 monthly.

    Monthly means the next calendar-month anniversary, clamped to month end.
    Membership in a cadence window is recomputed today, even at year boundaries.
    None means no refresh cadence (future or older than twelve months).
    """
    day = _day(today)
    age = _month(day.strftime("%Y%m")) - _month(ym)
    if age < 0 or age > 12:
        return None
    if observed_at is None:
        return day
    observed = _day(observed_at)
    if observed > day:
        raise ValueError("future observation")
    if age == 0:
        return observed + timedelta(days=1)
    if age <= 3:
        return observed + timedelta(days=7)
    month = _month(observed.strftime("%Y%m")) + 1
    year, zero_month = divmod(month, 12)
    return date(year, zero_month + 1,
                min(observed.day, calendar.monthrange(year, zero_month + 1)[1]))


def plan_day(lawd_codes: Iterable[str | Mapping], *, today: date | datetime,
             coverage: Mapping[Pair, date | datetime],
             sale: ServiceRange = ServiceRange("200601"),
             rent: ServiceRange = ServiceRange("201101"),
             budget: Budget = Budget()) -> DailyPlan:
    """Return an ordered, duplicate-free plan independent of input iteration order.

    Refresh and audit cannot consume each other's shares. Unused slots and integer
    rounding residue flow to backfill, which cannot exceed joint daily capacity.
    Within each refresh month, missing then oldest observations go first, with
    code ties. Audit picks oldest observed old pairs (not missing history), with
    newest-month/code ties, skipping pairs already observed today. Callers feed
    successful observations back as coverage to advance these pure input cursors.
    Replanning does not imply reservations or consumption;
    callers must subtract already reserved requests from each service's budget.
    """
    day = _day(today)
    current = _month(day.strftime("%Y%m"))
    codes = set()
    for item in lawd_codes:
        if isinstance(item, str):
            code = item
        else:
            if type(item.get("active")) is not bool:
                raise ValueError("active must be boolean")
            if not item["active"]:
                continue
            code = item["code"]
        if not isinstance(code, str) or not re.fullmatch(r"[0-9]{5}", code) or code.endswith("000"):
            raise ValueError("expected non-sido five-digit LAWD code")
        codes.add(code)
    ordered_codes = sorted(codes)
    observed = {}
    for (code, ym), value in coverage.items():
        _month(ym)
        stamp = _day(value)
        if stamp > day:
            raise ValueError("future observation")
        observed[(code, ym)] = stamp
    start = max(_month(sale.start), _month(rent.start))
    end = min(current, _month(sale.end) if sale.end else current,
              _month(rent.end) if rent.end else current)
    share = budget.lane_capacity(budget.refresh_percent)
    capacity = budget.pair_capacity
    if start > end:
        return DailyPlan(day, (), capacity, 0, share, 0, 0, None, None)
    # Observation dates form a deterministic cursor even with varying budgets.
    # Missing observations precede all recorded dates (including date.min).
    refresh = []
    for month in range(end, max(start, current - 12) - 1, -1):
        ym = _ym(month)
        oldest_codes = sorted(ordered_codes, key=lambda code: (
            (code, ym) in observed, observed.get((code, ym), date.min), code))
        for code in oldest_codes:
            due = refresh_due_on(ym, observed.get((code, ym)), day)
            if due is not None and due <= day:
                refresh.append((code, ym))
    items = [PlannedPair(*pair, "refresh") for pair in refresh[:share]]
    selected = {item.pair for item in items}
    audit = sorted((pair for pair, stamp in observed.items()
                    if pair[0] in codes and start <= _month(pair[1]) <= min(end, current - 13)
                    and stamp < day),
                   key=lambda pair: (observed[pair], -_month(pair[1]), pair[0]))
    for pair in audit[:min(budget.lane_capacity(budget.audit_percent), capacity - len(items))]:
        items.append(PlannedPair(*pair, "audit"))
        selected.add(pair)
    # Seoul's full common history precedes any nationwide backfill.
    for seoul in (True, False):
        for month in range(end, start - 1, -1):
            for code in ordered_codes:
                if len(items) >= capacity:
                    break
                pair = (code, _ym(month))
                if code.startswith("11") == seoul and pair not in observed and pair not in selected:
                    items.append(PlannedPair(*pair, "backfill"))
                    selected.add(pair)
            if len(items) >= capacity:
                break
    return DailyPlan(day, tuple(items), capacity, len(refresh), share,
                     max(0, len(refresh) - share), len(items) * budget.requests_per_pair,
                     _ym(start), _ym(end))
