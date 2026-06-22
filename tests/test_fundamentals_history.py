"""Branch coverage for the local official-fundamentals history store.

The store is exercised end-to-end via test_official_provider; these tests pin
the helper functions and the load/merge/prune/parse edge branches directly
(corrupt files, period guards, pruning) that the integration path doesn't hit.
"""

import json

from backend.adapters.fundamentals_history import (
    HistoricalQuarterlyFundamental,
    OfficialFundamentalsHistoryStore,
    _margin_delta,
    _period,
    _period_key,
    _yoy,
)
from backend.adapters.official_fundamentals import (
    OfficialBalanceSheetRow,
    OfficialIncomeStatementRow,
)


def _income(stock_code="9999", fiscal_year=None, quarter=None):
    return OfficialIncomeStatementRow(
        stockCode=stock_code,
        companyName="測試",
        market="TWSE",
        fiscalYear=fiscal_year,
        quarter=quarter,
        revenue=None,
        costOfRevenue=None,
        grossProfit=None,
        grossMargin=None,
        operatingIncome=None,
        operatingMargin=None,
        netIncome=None,
        eps=None,
        source="test",
    )


def _balance(stock_code="9999", fiscal_year=None, quarter=None):
    return OfficialBalanceSheetRow(
        stockCode=stock_code,
        companyName="測試",
        market="TWSE",
        fiscalYear=fiscal_year,
        quarter=quarter,
        inventory=None,
        source="test",
    )


def _write(path, quarters):
    path.write_text(
        json.dumps({"schemaVersion": 1, "updatedAt": None, "quarters": quarters}, ensure_ascii=False),
        encoding="utf-8",
    )


# --- helpers ----------------------------------------------------------------


def test_period_guards_none_and_out_of_range():
    assert _period(2025, 1) == "2025Q1"
    assert _period(None, 1) is None
    assert _period(2025, None) is None
    assert _period(2025, 0) is None
    assert _period(2025, 5) is None


def test_period_key_falls_back_on_unparseable():
    assert _period_key("2025Q3") == (2025, 3)
    assert _period_key("not-a-period") == (0, 0)


def test_yoy_and_margin_delta_guards():
    assert _yoy(10.0, 5.0) == 100.0
    assert _yoy(None, 5.0) is None
    assert _yoy(5.0, None) is None
    assert _yoy(5.0, 0) is None
    assert _margin_delta(5.0, 2.0) == 3.0
    assert _margin_delta(None, 2.0) is None
    assert _margin_delta(5.0, None) is None


def test_historical_quarter_epsyoy_property_is_none():
    row = HistoricalQuarterlyFundamental(stockCode="9999", period="2025Q4", fiscalYear=2025, quarter=4)
    assert row.epsYoY is None


# --- load() recovery branches -----------------------------------------------


def test_load_recovers_from_corrupt_json(tmp_path):
    path = tmp_path / "history.json"
    path.write_text("{not valid json", encoding="utf-8")
    assert OfficialFundamentalsHistoryStore(path).load() == {"schemaVersion": 1, "updatedAt": None, "quarters": {}}


def test_load_rejects_non_dict_payload(tmp_path):
    path = tmp_path / "history.json"
    path.write_text("[]", encoding="utf-8")
    assert OfficialFundamentalsHistoryStore(path).load()["quarters"] == {}


def test_load_normalizes_non_dict_quarters(tmp_path):
    path = tmp_path / "history.json"
    path.write_text(json.dumps({"quarters": "not-a-dict"}), encoding="utf-8")
    assert OfficialFundamentalsHistoryStore(path).load()["quarters"] == {}


# --- merge / query / prune branches -----------------------------------------


def test_merge_skips_rows_without_a_resolvable_period(tmp_path):
    store = OfficialFundamentalsHistoryStore(tmp_path / "history.json")
    status = store.merge_rows([_income(fiscal_year=None, quarter=None)], [_balance(fiscal_year=None, quarter=None)])
    assert status["rows"] == 0
    assert status["companies"] == 0


def test_quarter_returns_none_for_none_period(tmp_path):
    store = OfficialFundamentalsHistoryStore(tmp_path / "history.json")
    assert store.quarter("9999", None) is None


def test_annual_financials_handles_non_dict_company_records(tmp_path):
    path = tmp_path / "history.json"
    _write(path, {"9999": "not-a-dict"})
    assert OfficialFundamentalsHistoryStore(path).annual_financials("9999") == []


def test_annual_financials_skips_q4_record_missing_fiscal_year(tmp_path):
    path = tmp_path / "history.json"
    _write(path, {"9999": {"2025Q4": {"quarter": 4, "netIncome": 100.0}}})
    assert OfficialFundamentalsHistoryStore(path).annual_financials("9999") == []


def test_to_quarter_returns_none_when_record_missing_fiscal_year(tmp_path):
    path = tmp_path / "history.json"
    _write(path, {"9999": {"2025Q4": {"quarter": 4}}})
    assert OfficialFundamentalsHistoryStore(path).quarter("9999", "2025Q4") is None


def test_prune_drops_bad_companies_and_aged_out_periods(tmp_path):
    path = tmp_path / "history.json"
    _write(
        path,
        {
            "AAAA": "not-a-dict",
            "BBBB": {},
            "CCCC": {
                "1990Q4": {"fiscalYear": 1990, "quarter": 4, "netIncome": 1.0},
                "2025Q4": {"fiscalYear": 2025, "quarter": 4, "netIncome": 2.0},
            },
        },
    )
    store = OfficialFundamentalsHistoryStore(path, max_years=2)
    status = store.merge_rows([], [])  # triggers _prune over the seeded quarters
    assert status["companies"] == 1  # AAAA (non-dict) and BBBB (empty) dropped
    assert status["rows"] == 1  # CCCC's 1990Q4 aged out, only 2025Q4 remains


def test_status_reports_latest_period_and_expected_period_coverage(tmp_path):
    path = tmp_path / "history.json"
    _write(
        path,
        {
            "2330": {"2025Q4": {"fiscalYear": 2025, "quarter": 4}, "2026Q1": {"fiscalYear": 2026, "quarter": 1}},
            "2317": {"2026Q1": {"fiscalYear": 2026, "quarter": 1}},
            "1101": {"2025Q4": {"fiscalYear": 2025, "quarter": 4}},
        },
    )

    status = OfficialFundamentalsHistoryStore(path).status(expected_period="2026Q1")

    assert status["latestFinancialPeriod"] == "2026Q1"
    assert status["periodCoverage"] == {"2025Q4": 2, "2026Q1": 2}
    assert status["expectedPeriodCoverage"] == 2
