"""Trading-day awareness for the Worker's cache freshness policy.

Every source the seed is built from - daily valuation ratios, company profiles, MOPS
monthly revenue and quarterly filings - only publishes on a trading day. Freshness is
decided by the seed builder's ``nextRefreshAfter`` slot in the manifest
(``manifest_next_refresh`` below); this module supplies the trading-day and filing-window
awareness that both runtimes need around that: ``is_trading_day`` and ``refresh_reason``.

The Worker cannot import ``backend``, so these are duplicated and
``tests/test_trading_calendar_parity.py`` pins both implementations to the same answers.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

# Taiwan has had no daylight saving since 1979, so a fixed offset is exact here and
# avoids zoneinfo, which the Worker runtime does not carry.
TAIPEI_TZ = timezone(timedelta(hours=8))


def parse_closed_dates(values) -> set:
    """Read the manifest's ``marketClosedDates``; anything unparseable is ignored.

    A missing or broken list degrades to weekend-only, which still removes the large
    majority of non-trading days rather than failing the request.
    """
    closed: set = set()
    if not isinstance(values, (list, tuple, set)):
        return closed
    for value in values:
        try:
            closed.add(date.fromisoformat(str(value)[:10]))
        except (TypeError, ValueError):
            continue
    return closed


def is_trading_day(day: date, closed_dates=frozenset()) -> bool:
    return day.weekday() < 5 and day not in closed_dates


# The seed builder (backend.services.cache_policy.next_refresh_after) writes the next
# publication slot into the manifest; the Worker trusts it inside these bounds.
MAX_TRUSTED_REFRESH_SPAN = timedelta(days=21)


def manifest_next_refresh(manifest, generated_time: datetime):
    value = (manifest or {}).get("nextRefreshAfter")
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or not generated_time < parsed <= generated_time + MAX_TRUSTED_REFRESH_SPAN:
        return None
    return parsed.astimezone(UTC)


# Statutory filing windows, mirroring backend.services.filing_calendar/cache_policy:
# (general deadline, final financial-industry deadline, lead days before the general one).
FILING_DEADLINES = (((3, 31), (3, 31), 30), ((5, 15), (5, 30), 14), ((8, 14), (8, 31), 14), ((11, 14), (11, 29), 14))
MONTHLY_REVENUE_DEADLINE_DAY = 10


def next_open_day(day: date, closed_dates=frozenset()) -> date:
    while not is_trading_day(day, closed_dates):
        day += timedelta(days=1)
    return day


def refresh_reason(day: date, closed_dates=frozenset()) -> str:
    for (g_month, g_day), (f_month, f_day), lead in FILING_DEADLINES:
        start = date(day.year, g_month, g_day) - timedelta(days=lead)
        if start <= day <= next_open_day(date(day.year, f_month, f_day), closed_dates):
            return "financial_report_window"
    if day <= next_open_day(date(day.year, day.month, MONTHLY_REVENUE_DEADLINE_DAY), closed_dates):
        return "monthly_revenue_window"
    return "routine_refresh"
