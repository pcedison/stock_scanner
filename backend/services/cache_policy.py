from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

TAIPEI_TZ = ZoneInfo("Asia/Taipei")

# 財報截止日（月, 日）—— 前後 3 天視為財報窗口
FINANCIAL_REPORT_DEADLINES: frozenset[tuple[int, int]] = frozenset(
    {(3, 31), (5, 15), (5, 30), (8, 31), (11, 14)}
)
MONTHLY_REVENUE_WINDOW_START_DAY = 8
MONTHLY_REVENUE_WINDOW_END_DAY = 15

# Every source the seed is built from - daily valuation ratios, company profiles, MOPS
# monthly revenue and quarterly filings - only publishes on a trading day. The market
# closes at 13:30 and the official post-close files land shortly after, so 15:00 Taipei
# is the point where a trading day's data is complete and worth rebuilding for.
TRADING_DAY_PUBLISH_HOUR = 15


def in_financial_window(now: datetime) -> bool:
    return any(
        month == now.month
        and abs((now.date() - now.replace(month=month, day=day).date()).days) <= 3
        for month, day in FINANCIAL_REPORT_DEADLINES
    )


def refresh_policy(now: datetime | None = None) -> dict:
    current = now or datetime.now(TAIPEI_TZ)
    if in_financial_window(current):
        return {
            "strategy": "stale_while_revalidate",
            "reason": "financial_report_window",
            "minIntervalSeconds": 7200,
        }
    if MONTHLY_REVENUE_WINDOW_START_DAY <= current.day <= MONTHLY_REVENUE_WINDOW_END_DAY:
        return {
            "strategy": "stale_while_revalidate",
            "reason": "monthly_revenue_window",
            "minIntervalSeconds": 10800,
        }
    return {
        "strategy": "stale_while_revalidate",
        "reason": "routine_refresh",
        "minIntervalSeconds": 43200,
    }


def is_trading_day(day: date, closed_dates: frozenset[date] | set[date] = frozenset()) -> bool:
    """Weekends are never trading days; `closed_dates` carries the TWSE holiday list."""
    return day.weekday() < 5 and day not in closed_dates


def next_publication_time(
    candidate: datetime, closed_dates: frozenset[date] | set[date] = frozenset()
) -> datetime:
    """Move a refresh deadline forward to when new data could actually exist.

    A deadline that already falls on a trading day is left alone, so the intraday cadence
    during the monthly-revenue and financial-report windows is unchanged. One that falls
    on a weekend or a market holiday is pushed to the next trading day's publish hour:
    nothing is filed while the market is shut, so treating the seed as stale there only
    produces false alarms and pointless fetches against sources that cannot have changed.
    """
    local = candidate.astimezone(TAIPEI_TZ)
    if is_trading_day(local.date(), closed_dates):
        return candidate
    day = local.date() + timedelta(days=1)
    while not is_trading_day(day, closed_dates):
        day += timedelta(days=1)
    publish = datetime.combine(day, time(TRADING_DAY_PUBLISH_HOUR, 0), tzinfo=TAIPEI_TZ)
    return publish.astimezone(candidate.tzinfo) if candidate.tzinfo else publish
