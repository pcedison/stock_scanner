from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from backend.services.filing_calendar import (
    financial_report_events,
    monthly_revenue_deadline,
)

TAIPEI_TZ = ZoneInfo("Asia/Taipei")

# When the official datasets the seed is built from actually change (Taipei time, observed
# from HTTP Last-Modified on 2026-09-14). None of them change intraday, so a rebuild between
# two regenerations only re-fetches identical data.
#
# - TWSE OpenAPI (t187ap03/05/06/07_L) is regenerated once a day at ~05:25 with the MOPS
#   filings up to the previous day.
MORNING_SLOT = time(6, 30)
# - TPEX OpenAPI (profiles, revenue, filings, P/E) is regenerated at ~16:00, and TWSE
#   BWIBBU_d carries the day's valuation ratios after the 13:30 close (confirmed present at
#   17:40; its exact publication time was not observed, so the slot waits until 18:00).
EVENING_SLOT = time(18, 0)

# Most companies file in the last two weeks before the general deadline; nothing arrives
# after the final (financial-industry) deadline.
FINANCIAL_WINDOW_LEAD_DAYS = 14
# Large caps (paid-in capital >= NT$10bn) must file the annual report within 75 days (~3/16).
ANNUAL_WINDOW_LEAD_DAYS = 30

# Cooldown between terminal refresh jobs (a failed rebuild is retried after this). Freshness
# itself is decided by `next_refresh_after`, not by this interval.
TERMINAL_JOB_COOLDOWN_SECONDS = 3600


def in_monthly_revenue_window(day: date) -> bool:
    """Day 1 through the (holiday-extended) 10th, when last month's revenue is being filed."""
    return day <= monthly_revenue_deadline(day.year, day.month)


def in_financial_window(day: date) -> bool:
    for event in financial_report_events(day.year):
        lead = ANNUAL_WINDOW_LEAD_DAYS if event.kind == "annual" else FINANCIAL_WINDOW_LEAD_DAYS
        if event.general_deadline - timedelta(days=lead) <= day <= event.final_deadline:
            return True
    return False


def refresh_policy(now: datetime | None = None) -> dict:
    current = (now or datetime.now(TAIPEI_TZ)).astimezone(TAIPEI_TZ)
    if in_financial_window(current.date()):
        reason = "financial_report_window"
    elif in_monthly_revenue_window(current.date()):
        reason = "monthly_revenue_window"
    else:
        reason = "routine_refresh"
    return {
        "strategy": "stale_while_revalidate",
        "reason": reason,
        "schedule": "publication_slots",
        "minIntervalSeconds": TERMINAL_JOB_COOLDOWN_SECONDS,
    }


def is_trading_day(day: date, closed_dates: frozenset[date] | set[date] = frozenset()) -> bool:
    """Weekends are never trading days; `closed_dates` carries the TWSE holiday list."""
    return day.weekday() < 5 and day not in closed_dates


def active_publication_slots(day: date, closed_dates: frozenset[date] | set[date] = frozenset()) -> list[time]:
    """Taipei times on ``day`` at which a rebuild can pick up newly published data.

    The evening slot is the daily rebuild. The morning TWSE batch only adds anything worth
    a rebuild while filings are pouring in, so it is used inside filing windows only.
    """
    if not is_trading_day(day, closed_dates):
        return []
    if in_financial_window(day) or in_monthly_revenue_window(day):
        return [MORNING_SLOT, EVENING_SLOT]
    return [EVENING_SLOT]


def next_refresh_after(
    generated_at: datetime, closed_dates: frozenset[date] | set[date] = frozenset()
) -> datetime:
    """The first publication slot after a build: the seed is fresh until then."""
    local = generated_at.astimezone(TAIPEI_TZ)
    day = local.date()
    for _ in range(60):  # longest market closure is ~10 days
        for slot in active_publication_slots(day, closed_dates):
            candidate = datetime.combine(day, slot, tzinfo=TAIPEI_TZ)
            if candidate > local:
                return candidate.astimezone(generated_at.tzinfo) if generated_at.tzinfo else candidate
        day += timedelta(days=1)
    raise ValueError(f"no trading day within 60 days after {generated_at.isoformat()}")
