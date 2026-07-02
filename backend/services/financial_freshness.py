from __future__ import annotations

from typing import Any


def _period_key(period: Any) -> tuple[int, int]:
    if not isinstance(period, str):
        return (0, 0)
    try:
        year_text, quarter_text = period.upper().split("Q", 1)
        year = int(year_text)
        quarter = int(quarter_text)
    except ValueError:
        return (0, 0)
    if quarter < 1 or quarter > 4:
        return (0, 0)
    return (year, quarter)


def _active_financial_period(context: dict[str, Any]) -> str | None:
    for field in ("freshnessFinancialReport", "activeFinancialReport"):
        active = context.get(field)
        if not isinstance(active, dict):
            continue
        period = active.get("period")
        if isinstance(period, str) and period:
            return period
    return None


def _int_or_zero(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def build_financial_freshness_status(
    filing_context: dict[str, Any],
    history_status: dict[str, Any] | None,
) -> dict[str, Any]:
    history = history_status if isinstance(history_status, dict) else {}
    expected_period = _active_financial_period(filing_context)
    latest_period = history.get("latestFinancialPeriod")
    latest_period = latest_period if isinstance(latest_period, str) and latest_period else None
    expected_coverage = _int_or_zero(history.get("expectedPeriodCoverage"))

    is_fresh = bool(expected_period and latest_period and _period_key(latest_period) >= _period_key(expected_period))
    coverage_status = "ok" if expected_coverage > 0 else "low"
    blocks_deployment = False

    if expected_period is None:
        status = "pending"
        message = "目前沒有可判定的財報申報期別。"
    elif not is_fresh:
        status = "stale"
        coverage_status = "missing"
        blocks_deployment = True
        latest_label = latest_period or "無快取"
        message = f"財報快取落後：目前應覆蓋 {expected_period}，但最新快取僅 {latest_label}。"
    elif coverage_status == "low":
        status = "warning"
        message = f"財報快取期別已達 {expected_period}，但尚未統計到該期公司覆蓋數。"
    else:
        status = "ok"
        message = f"財報快取已覆蓋 {expected_period}，目前有 {expected_coverage} 檔公司資料。"

    return {
        "status": status,
        "isFresh": is_fresh,
        "blocksDeployment": blocks_deployment,
        "coverageStatus": coverage_status,
        "expectedFinancialPeriod": expected_period,
        "latestCachedFinancialPeriod": latest_period,
        "expectedPeriodCoverage": expected_coverage,
        "periodCoverage": history.get("periodCoverage") if isinstance(history.get("periodCoverage"), dict) else {},
        "historyRows": history.get("rows"),
        "historyCompanies": history.get("companies"),
        "historyUpdatedAt": history.get("updatedAt"),
        "expectedMonthlyRevenuePeriod": filing_context.get("monthlyRevenuePeriod"),
        "message": message,
    }
