from datetime import date

from backend.services.filing_calendar import filing_context
from backend.services.financial_freshness import build_financial_freshness_status


def _history_status(latest_period: str | None, expected_count: int = 0) -> dict:
    return {
        "enabled": True,
        "rows": 12,
        "companies": 6,
        "updatedAt": "2026-06-22T01:00:00+00:00",
        "latestFinancialPeriod": latest_period,
        "periodCoverage": {latest_period: 6} if latest_period else {},
        "expectedPeriodCoverage": expected_count,
    }


def test_financial_freshness_marks_stale_when_cache_lags_expected_period():
    status = build_financial_freshness_status(
        filing_context(date(2026, 6, 22)),
        _history_status("2025Q4"),
    )

    assert status["status"] == "stale"
    assert status["isFresh"] is False
    assert status["expectedFinancialPeriod"] == "2026Q1"
    assert status["latestCachedFinancialPeriod"] == "2025Q4"
    assert status["blocksDeployment"] is True


def test_financial_freshness_marks_ok_when_cache_reaches_expected_period():
    status = build_financial_freshness_status(
        filing_context(date(2026, 6, 22)),
        _history_status("2026Q1", expected_count=5),
    )

    assert status["status"] == "ok"
    assert status["isFresh"] is True
    assert status["coverageStatus"] == "ok"
    assert status["expectedPeriodCoverage"] == 5
    assert status["blocksDeployment"] is False


def test_financial_freshness_does_not_block_before_active_period_deadline():
    status = build_financial_freshness_status(
        filing_context(date(2026, 7, 1)),
        _history_status("2026Q1", expected_count=5),
    )

    assert status["status"] == "ok"
    assert status["isFresh"] is True
    assert status["expectedFinancialPeriod"] == "2026Q1"
    assert status["latestCachedFinancialPeriod"] == "2026Q1"
    assert status["blocksDeployment"] is False


def test_financial_freshness_blocks_after_active_period_deadline():
    status = build_financial_freshness_status(
        filing_context(date(2026, 9, 1)),
        _history_status("2026Q1", expected_count=5),
    )

    assert status["status"] == "stale"
    assert status["isFresh"] is False
    assert status["expectedFinancialPeriod"] == "2026Q2"
    assert status["latestCachedFinancialPeriod"] == "2026Q1"
    assert status["blocksDeployment"] is True


def test_financial_freshness_warns_when_period_is_fresh_but_coverage_is_low():
    status = build_financial_freshness_status(
        filing_context(date(2026, 6, 22)),
        _history_status("2026Q1", expected_count=0),
    )

    assert status["status"] == "warning"
    assert status["isFresh"] is True
    assert status["coverageStatus"] == "low"
    assert status["blocksDeployment"] is False
