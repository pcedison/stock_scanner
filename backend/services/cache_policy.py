from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

TAIPEI_TZ = ZoneInfo("Asia/Taipei")

# 財報截止日（月, 日）—— 前後 3 天視為財報窗口
FINANCIAL_REPORT_DEADLINES: frozenset[tuple[int, int]] = frozenset(
    {(3, 31), (5, 15), (5, 30), (8, 31), (11, 14)}
)
MONTHLY_REVENUE_WINDOW_START_DAY = 8
MONTHLY_REVENUE_WINDOW_END_DAY = 15


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
