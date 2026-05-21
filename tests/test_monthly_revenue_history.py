from __future__ import annotations

from backend.adapters.monthly_revenue_history import MonthlyRevenueHistoryStore, _month_key, _prev_month
from backend.adapters.official_monthly_revenue import OfficialMonthlyRevenueRow


def _row(stock_code: str, data_month: str, yoy: float | None = None, rev: int | None = None, cum_yoy: float | None = None) -> OfficialMonthlyRevenueRow:
    return OfficialMonthlyRevenueRow(
        stockCode=stock_code,
        companyName="Test",
        market="TWSE",
        industryName="",
        reportDate="",
        dataMonth=data_month,
        monthlyRevenue=rev,
        monthlyRevenueYoY=yoy,
        cumulativeRevenue=rev,
        cumulativeRevenueYoY=cum_yoy,
    )


def test_month_key_sorts_correctly():
    months = ["2026-04", "2025-12", "2026-01", "2025-01"]
    assert sorted(months, key=_month_key) == ["2025-01", "2025-12", "2026-01", "2026-04"]


def test_prev_month_basic():
    assert _prev_month("2026-04") == "2026-03"
    assert _prev_month("2026-01") == "2025-12"


def test_prev_month_invalid():
    assert _prev_month("invalid") == ""
    assert _prev_month("") == ""


def test_merge_rows_persists_data(tmp_path):
    store = MonthlyRevenueHistoryStore(tmp_path / "rev_history.json")
    rows = [
        _row("1101", "2026-04", yoy=15.2, rev=10_000_000),
        _row("2330", "2026-04", yoy=22.5, rev=20_000_000),
    ]
    status = store.merge_rows(rows)

    assert status["updatedRows"] == 2
    assert status["companies"] == 2

    data = store.load()
    assert data["months"]["1101"]["2026-04"]["monthlyRevenueYoY"] == 15.2
    assert data["months"]["2330"]["2026-04"]["monthlyRevenue"] == 20_000_000


def test_merge_rows_idempotent(tmp_path):
    store = MonthlyRevenueHistoryStore(tmp_path / "rev_history.json")
    row = _row("1101", "2026-04", yoy=15.2)
    store.merge_rows([row])
    status = store.merge_rows([row])
    assert status["updatedRows"] == 0


def test_previous_month_yoy_returns_last_month(tmp_path):
    store = MonthlyRevenueHistoryStore(tmp_path / "rev_history.json")
    store.merge_rows([
        _row("1101", "2026-03", yoy=10.5),
        _row("1101", "2026-04", yoy=15.2),
    ])

    assert store.previous_month_yoy("1101", "2026-04") == 10.5
    assert store.previous_month_yoy("1101", "2026-01") is None  # no 2025-12
    assert store.previous_month_yoy("9999", "2026-04") is None  # unknown stock


def test_previous_month_yoy_across_year(tmp_path):
    store = MonthlyRevenueHistoryStore(tmp_path / "rev_history.json")
    store.merge_rows([_row("1101", "2025-12", yoy=8.0)])

    assert store.previous_month_yoy("1101", "2026-01") == 8.0


def test_trailing_three_month_avg_yoy_needs_two_data_points(tmp_path):
    store = MonthlyRevenueHistoryStore(tmp_path / "rev_history.json")
    store.merge_rows([_row("1101", "2026-04", yoy=15.0)])

    assert store.trailing_three_month_avg_yoy("1101", "2026-04") is None  # only 1 point


def test_trailing_three_month_avg_yoy_two_points(tmp_path):
    store = MonthlyRevenueHistoryStore(tmp_path / "rev_history.json")
    store.merge_rows([
        _row("1101", "2026-03", yoy=10.0),
        _row("1101", "2026-04", yoy=20.0),
    ])

    avg = store.trailing_three_month_avg_yoy("1101", "2026-04")
    assert avg == 15.0


def test_trailing_three_month_avg_yoy_three_points(tmp_path):
    store = MonthlyRevenueHistoryStore(tmp_path / "rev_history.json")
    store.merge_rows([
        _row("1101", "2026-02", yoy=12.0),
        _row("1101", "2026-03", yoy=18.0),
        _row("1101", "2026-04", yoy=24.0),
    ])

    avg = store.trailing_three_month_avg_yoy("1101", "2026-04")
    assert avg == 18.0


def test_jan_feb_combined_yoy_requires_four_data_points(tmp_path):
    store = MonthlyRevenueHistoryStore(tmp_path / "rev_history.json")
    # Only current year data, no previous year
    store.merge_rows([
        _row("1101", "2026-01", rev=10_000_000),
        _row("1101", "2026-02", rev=12_000_000),
    ])

    assert store.jan_feb_combined_yoy("1101", 2026) is None


def test_jan_feb_combined_yoy_computed_correctly(tmp_path):
    store = MonthlyRevenueHistoryStore(tmp_path / "rev_history.json")
    store.merge_rows([
        _row("1101", "2025-01", rev=8_000_000),
        _row("1101", "2025-02", rev=9_000_000),
        _row("1101", "2026-01", rev=10_000_000),
        _row("1101", "2026-02", rev=11_000_000),
    ])

    yoy = store.jan_feb_combined_yoy("1101", 2026)
    # (10M + 11M) / (8M + 9M) - 1 = 21M / 17M - 1 ≈ 23.53%
    assert yoy is not None
    assert abs(yoy - (21_000_000 / 17_000_000 - 1) * 100) < 0.01


def test_jan_feb_combined_yoy_zero_prev_returns_none(tmp_path):
    store = MonthlyRevenueHistoryStore(tmp_path / "rev_history.json")
    store.merge_rows([
        _row("1101", "2025-01", rev=0),
        _row("1101", "2025-02", rev=0),
        _row("1101", "2026-01", rev=10_000_000),
        _row("1101", "2026-02", rev=11_000_000),
    ])

    assert store.jan_feb_combined_yoy("1101", 2026) is None


def test_prune_keeps_max_months(tmp_path):
    store = MonthlyRevenueHistoryStore(tmp_path / "rev_history.json")
    # Insert 16 months (> MAX_MONTHS_PER_COMPANY=14)
    rows = [_row("1101", f"2025-{m:02d}", yoy=float(m)) for m in range(1, 13)]
    rows += [_row("1101", f"2026-{m:02d}", yoy=float(m + 12)) for m in range(1, 5)]
    store.merge_rows(rows)

    data = store.load()
    company_months = data["months"]["1101"]
    assert len(company_months) == 14
    # Oldest months should be gone
    assert "2025-01" not in company_months
    assert "2025-02" not in company_months
    # Newest should be present
    assert "2026-04" in company_months


def test_status_reflects_stored_data(tmp_path):
    store = MonthlyRevenueHistoryStore(tmp_path / "rev_history.json")
    assert store.status()["companies"] == 0

    store.merge_rows([_row("1101", "2026-04", yoy=15.0), _row("2330", "2026-04", yoy=20.0)])
    s = store.status()
    assert s["companies"] == 2
    assert s["rows"] == 2
    assert s["updatedAt"] is not None


def test_load_handles_missing_file(tmp_path):
    store = MonthlyRevenueHistoryStore(tmp_path / "nonexistent.json")
    data = store.load()
    assert data["months"] == {}
    assert data["schemaVersion"] == 1


def test_written_json_is_readable_format(tmp_path):
    store = MonthlyRevenueHistoryStore(tmp_path / "rev_history.json")
    store.merge_rows([_row("1101", "2026-04", yoy=15.2)])
    content = (tmp_path / "rev_history.json").read_text(encoding="utf-8")
    # indent=2 means content has newlines and spaces
    assert "\n" in content
    assert "  " in content
