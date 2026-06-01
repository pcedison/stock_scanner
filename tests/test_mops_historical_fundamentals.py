from backend.adapters.fundamentals_history import OfficialFundamentalsHistoryStore
from backend.adapters.mops_historical_fundamentals import OfficialMopsHistoricalFundamentalsAdapter
from backend.adapters.official_fundamentals import OfficialBalanceSheetRow, OfficialIncomeStatementRow
from backend.models.company import Company
from backend.services.official_history_backfill import (
    BackfillProgressStore,
    OfficialHistoryBackfillResult,
    OfficialHistoryBackfillService,
    _backfill_periods,
    _dedupe_periods,
    _full_quarterly_periods,
    _latest_annual_year,
    _strategy_backfill_periods,
)

INCOME_HTML = """
<table>
<tr><td>會計項目</td><td>114年01月01日至114年03月31日</td><td>113年01月01日至113年03月31日</td></tr>
<tr><td></td><td>金額</td><td>%</td><td>金額</td><td>%</td></tr>
<tr><td>營業收入合計</td><td>200</td><td>100.00</td><td>100</td><td>100.00</td></tr>
<tr><td>營業成本合計</td><td>80</td><td>40.00</td><td>50</td><td>50.00</td></tr>
<tr><td>營業毛利（毛損）淨額</td><td>120</td><td>60.00</td><td>50</td><td>50.00</td></tr>
<tr><td>營業利益（損失）</td><td>90</td><td>45.00</td><td>30</td><td>30.00</td></tr>
<tr><td>本期淨利（淨損）</td><td>70</td><td>35.00</td><td>20</td><td>20.00</td></tr>
<tr><td>母公司業主（淨利∕損）</td><td>60</td><td>30.00</td><td>10</td><td>10.00</td></tr>
<tr><td>基本每股盈餘</td><td></td><td></td><td></td><td></td></tr>
<tr><td>基本每股盈餘</td><td>6.0</td><td></td><td>3.0</td><td></td></tr>
</table>
"""


BALANCE_HTML = """
<table>
<tr><td>會計項目</td><td>114年03月31日</td><td>113年12月31日</td><td>113年03月31日</td></tr>
<tr><td></td><td>金額</td><td>%</td><td>金額</td><td>%</td><td>金額</td><td>%</td></tr>
<tr><td>存貨</td><td>40</td><td>10.00</td><td>38</td><td>9.00</td><td>32</td><td>8.00</td></tr>
</table>
"""

INCOME_PAYLOAD = {
    "companyAbbreviation": "測試",
    "titles": [
        {"main": "會計項目", "sub": []},
        {"main": "114年度", "sub": [{"main": "金額", "sub": []}, {"main": "%", "sub": []}]},
        {"main": "113年度", "sub": [{"main": "金額", "sub": []}, {"main": "%", "sub": []}]},
    ],
    "reportList": [
        ["營業收入合計", "200", "100.00", "100", "100.00"],
        ["營業成本合計", "80", "40.00", "50", "50.00"],
        ["營業毛利（毛損）淨額", "120", "60.00", "50", "50.00"],
        ["營業利益（損失）", "90", "45.00", "30", "30.00"],
        ["　母公司業主（淨利∕損）", "60", "30.00", "10", "10.00"],
        ["　基本每股盈餘", "6.0", "", "3.0", ""],
    ],
}

BALANCE_PAYLOAD = {
    "companyAbbreviation": "測試",
    "titles": [
        {"main": "會計項目", "sub": []},
        {"main": "114年12月31日", "sub": [{"main": "金額", "sub": []}, {"main": "%", "sub": []}]},
        {"main": "113年12月31日", "sub": [{"main": "金額", "sub": []}, {"main": "%", "sub": []}]},
    ],
    "reportList": [["　　　存貨", "40", "10.00", "32", "8.00"]],
}


def test_mops_historical_parser_extracts_income_and_balance_rows():
    adapter = OfficialMopsHistoricalFundamentalsAdapter()

    incomes = adapter.parse_income_statement(INCOME_HTML, "9999", "測試公司", "TWSE")
    balances = adapter.parse_balance_sheet(BALANCE_HTML, "9999", "測試公司", "TWSE")

    assert [row.fiscalYear for row in incomes] == [2025, 2024]
    assert [row.quarter for row in incomes] == [1, 1]
    assert incomes[0].eps == 6.0
    assert incomes[1].netIncome == 10.0
    assert incomes[0].grossMargin == 60.0
    assert [row.inventory for row in balances] == [40.0, 38.0, 32.0]


def test_mops_official_api_payload_parser_extracts_income_and_balance_rows():
    adapter = OfficialMopsHistoricalFundamentalsAdapter()

    incomes = adapter.parse_income_payload(INCOME_PAYLOAD, "9999", "測試", "TWSE")
    balances = adapter.parse_balance_payload(BALANCE_PAYLOAD, "9999", "測試", "TWSE")

    assert [row.fiscalYear for row in incomes] == [2025, 2024]
    assert [row.quarter for row in incomes] == [4, 4]
    assert incomes[0].netIncome == 60.0
    assert incomes[0].eps == 6.0
    assert balances[0].inventory == 40.0


def test_mops_parsers_return_empty_when_header_row_missing():
    adapter = OfficialMopsHistoricalFundamentalsAdapter()
    no_header = "<table><tr><td>其他</td><td>1</td></tr></table>"
    assert adapter.parse_income_statement(no_header, "9999", "測試", "TWSE") == []
    assert adapter.parse_balance_sheet(no_header, "9999", "測試", "TWSE") == []


def test_mops_html_parsers_skip_unparseable_period_columns():
    adapter = OfficialMopsHistoricalFundamentalsAdapter()
    income = "<table><tr><td>會計項目</td><td>亂碼</td></tr><tr><td>營業收入合計</td><td>1</td></tr></table>"
    balance = "<table><tr><td>會計項目</td><td>亂碼</td></tr><tr><td>存貨</td><td>1</td></tr></table>"
    assert adapter.parse_income_statement(income, "9999", "測試", "TWSE") == []
    assert adapter.parse_balance_sheet(balance, "9999", "測試", "TWSE") == []


def test_mops_payload_parsers_reject_non_list_report_or_titles():
    adapter = OfficialMopsHistoricalFundamentalsAdapter()
    assert adapter.parse_income_payload({"reportList": "x", "titles": []}, "9999", "測試", "TWSE") == []
    assert adapter.parse_balance_payload({"reportList": [], "titles": "x"}, "9999", "測試", "TWSE") == []


def test_mops_payload_parsers_skip_unparseable_and_duplicate_periods():
    adapter = OfficialMopsHistoricalFundamentalsAdapter()
    income = {
        "companyAbbreviation": "測試",
        "titles": [{"main": "會計項目"}, {"main": "亂碼"}, {"main": "114年度"}, {"main": "114年度"}],
        "reportList": [["營業收入合計", "1", "", "2", "", "3", ""]],
    }
    balance = {
        "companyAbbreviation": "測試",
        "titles": [{"main": "會計項目"}, {"main": "亂碼"}, {"main": "114年12月31日"}, {"main": "114年12月31日"}],
        "reportList": [["存貨", "1", "", "2", "", "3", ""]],
    }
    # 亂碼 -> None (skipped), first 114 parsed, the duplicate 114 skipped via the seen-set.
    assert [row.fiscalYear for row in adapter.parse_income_payload(income, "9999", "測試", "TWSE")] == [2025]
    assert [row.fiscalYear for row in adapter.parse_balance_payload(balance, "9999", "測試", "TWSE")] == [2025]


def test_history_store_merges_mops_rows_and_derives_yoy(tmp_path):
    adapter = OfficialMopsHistoricalFundamentalsAdapter()
    store = OfficialFundamentalsHistoryStore(tmp_path / "history.json")
    incomes = adapter.parse_income_statement(INCOME_HTML, "9999", "測試公司", "TWSE")
    balances = adapter.parse_balance_sheet(BALANCE_HTML, "9999", "測試公司", "TWSE")

    status = store.merge_rows(incomes, balances)
    yoy = store.quarterly_yoy("9999", "2025Q1")

    assert status["rows"] == 3
    assert yoy["epsYoY"] == 100.0
    assert yoy["netIncomeYoY"] == 500.0
    assert yoy["grossMarginYoY"] == 10.0
    assert store.inventory_turnover("9999", "2025Q1") == 8.0


class FakeMopsBackfillAdapter:
    def fetch_company_period(self, stock_code, company_name, market, fiscal_year, quarter):
        return type(
            "Bundle",
            (),
            {
                "incomes": [
                    OfficialIncomeStatementRow(
                        stockCode=stock_code,
                        companyName=company_name,
                        market=market,
                        fiscalYear=fiscal_year,
                        quarter=quarter,
                        revenue=100.0,
                        costOfRevenue=60.0,
                        grossProfit=40.0,
                        grossMargin=40.0,
                        operatingIncome=20.0,
                        operatingMargin=20.0,
                        netIncome=10.0,
                        eps=1.0,
                        source="fake",
                    )
                ],
                "balances": [
                    OfficialBalanceSheetRow(
                        stockCode=stock_code,
                        companyName=company_name,
                        market=market,
                        fiscalYear=fiscal_year,
                        quarter=quarter,
                        inventory=30.0,
                        source="fake",
                    )
                ],
            },
        )()


class FakePendingCurrentAdapter(FakeMopsBackfillAdapter):
    def fetch_company_period(self, stock_code, company_name, market, fiscal_year, quarter):
        if fiscal_year == 2026 and quarter == 1:
            return type("Bundle", (), {"incomes": [], "balances": []})()
        return super().fetch_company_period(stock_code, company_name, market, fiscal_year, quarter)


def test_backfill_period_generation_covers_strategy_and_full_quarters():
    assert _strategy_backfill_periods(2026, 1, years=5) == [(2026, 1), (2025, 1), (2025, 4), (2023, 4), (2021, 4)]
    full = _full_quarterly_periods(2026, 1, years=5)

    assert full[:2] == [(2026, 1), (2025, 4)]
    assert (2021, 1) in full
    assert len(full) == 21


def test_backfill_service_writes_progress_and_resumes(tmp_path):
    companies = [
        Company(stockCode="1111", name="A", market="TWSE", industryName="Tech", isFinancial=False),
        Company(stockCode="2222", name="B", market="TWSE", industryName="Tech", isFinancial=False),
    ]
    service = OfficialHistoryBackfillService(
        history_store=OfficialFundamentalsHistoryStore(tmp_path / "history.json"),
        adapter=FakeMopsBackfillAdapter(),
        progress_store=BackfillProgressStore(tmp_path / "progress.json"),
    )

    first = service.backfill(companies, limit=1, throttle_seconds=0, reset_progress=True)
    second = service.backfill(companies, limit=10, throttle_seconds=0)

    assert first.progress["completedCompanies"] == 1
    assert first.completed is False
    assert second.progress["completedCompanies"] == 2
    assert second.completed is True
    assert service.history_store.status()["companies"] == 2


def test_backfill_service_treats_current_unannounced_period_as_pending(monkeypatch, tmp_path):
    # Pin the active filing window so FakePendingCurrentAdapter's blanked 2026Q1
    # is the current period regardless of the wall-clock date the suite runs on.
    _patch_filing_context(
        monkeypatch,
        {"activeFinancialReport": {"fiscalYear": 2026, "quarter": 1}, "monthlyRevenuePeriod": "2026-03"},
    )
    companies = [Company(stockCode="1111", name="A", market="TWSE", industryName="Tech", isFinancial=False)]
    service = OfficialHistoryBackfillService(
        history_store=OfficialFundamentalsHistoryStore(tmp_path / "history.json"),
        adapter=FakePendingCurrentAdapter(),
        progress_store=BackfillProgressStore(tmp_path / "progress.json"),
    )

    result = service.backfill(companies, limit=1, throttle_seconds=0, reset_progress=True)

    assert result.completed is True
    assert result.failedCompanies == 0
    assert result.pendingCompanies == 1
    assert result.progress["pendingCompanies"] == 1


class _FakeRow:
    def __init__(self, *, netIncome=10.0, eps=1.0, inventory=30.0, costOfRevenue=60.0):
        self.netIncome = netIncome
        self.eps = eps
        self.inventory = inventory
        self.costOfRevenue = costOfRevenue


class FakeHistoryStore:
    """Minimal history store stub for exercising backfill decision branches."""

    def __init__(self, *, quarter_row=None, annual_count=0):
        self._quarter_row = quarter_row
        self._annual_count = annual_count
        self.merged: list[tuple[list, list]] = []

    def quarter(self, stock_code, period):
        return self._quarter_row

    def annual_financials(self, stock_code):
        return [{"year": 2020 + i, "netIncome": 1.0} for i in range(self._annual_count)]

    def merge_rows(self, incomes, balances):
        self.merged.append((list(incomes), list(balances)))
        return {"companies": 1, "rows": len(incomes)}

    def status(self):
        return {"companies": 1, "rows": 0}


class AllEmptyAdapter:
    """Returns no rows for every requested period."""

    def fetch_company_period(self, stock_code, company_name, market, fiscal_year, quarter):
        return type("Bundle", (), {"incomes": [], "balances": []})()


def _company(stock_code="1111", *, is_financial=False):
    return Company(stockCode=stock_code, name="A", market="TWSE", industryName="Tech", isFinancial=is_financial)


def _patch_filing_context(monkeypatch, context):
    monkeypatch.setattr("backend.services.official_history_backfill.filing_context", lambda: context)


def _service(tmp_path, history_store, adapter, name="progress.json"):
    return OfficialHistoryBackfillService(
        history_store=history_store,
        adapter=adapter,
        progress_store=BackfillProgressStore(tmp_path / name),
    )


def test_period_helpers_dedupe_and_dispatch():
    assert _latest_annual_year(2026, 4) == 2026
    assert _latest_annual_year(2026, 1) == 2025
    assert _dedupe_periods([(2026, 1), (2026, 1), (2025, 4)]) == [(2026, 1), (2025, 4)]
    assert _backfill_periods(2026, 1, mode="full_quarterly") == _full_quarterly_periods(2026, 1)
    assert _backfill_periods(2026, 1) == _strategy_backfill_periods(2026, 1)
    # Unknown modes fall back to the strategy minimal set.
    assert _backfill_periods(2026, 1, mode="bogus") == _strategy_backfill_periods(2026, 1)


def test_progress_store_recovers_from_corruption_and_resets(tmp_path):
    path = tmp_path / "progress.json"
    store = BackfillProgressStore(path)
    assert store.load() == {}  # missing file

    path.write_text("{not valid json", encoding="utf-8")
    assert store.load() == {}  # corrupt JSON

    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert store.load() == {}  # valid JSON but not a dict

    store.save({"runKey": "x"})
    assert path.exists()
    store.reset()
    assert not path.exists()
    store.reset()  # no-op when already absent


def test_backfill_result_as_dict_round_trips():
    result = OfficialHistoryBackfillResult(
        requestedCompanies=1,
        backfilledCompanies=1,
        skippedCompanies=2,
        failedCompanies=0,
        pendingCompanies=0,
        incomeRows=3,
        balanceRows=4,
        historyStatus={"companies": 5},
        periods=["2026Q1"],
        progress={"runKey": "x"},
        completed=True,
    )
    payload = result.as_dict()
    assert payload["requestedCompanies"] == 1
    assert payload["periods"] == ["2026Q1"]
    assert payload["completed"] is True
    assert set(payload) == {
        "requestedCompanies",
        "backfilledCompanies",
        "skippedCompanies",
        "failedCompanies",
        "pendingCompanies",
        "incomeRows",
        "balanceRows",
        "historyStatus",
        "periods",
        "progress",
        "completed",
    }


def test_period_needs_backfill_branches(tmp_path):
    company = _company()
    financial = _company("2881", is_financial=True)
    adapter = FakeMopsBackfillAdapter()

    def needs(row, target=company):
        service = _service(tmp_path, FakeHistoryStore(quarter_row=row), adapter)
        return service._period_needs_backfill(target, 2025, 4)

    assert needs(None) is True  # no stored row at all
    assert needs(_FakeRow(netIncome=None)) is True
    assert needs(_FakeRow(eps=None)) is True
    assert needs(_FakeRow(inventory=None)) is True  # non-financial requires inventory
    assert needs(_FakeRow(costOfRevenue=None)) is True
    # Financial issuers do not need inventory / cost-of-revenue.
    assert needs(_FakeRow(inventory=None, costOfRevenue=None), target=financial) is False
    assert needs(_FakeRow()) is False  # fully populated non-financial


def test_needs_backfill_branches(tmp_path):
    company = _company()
    adapter = FakeMopsBackfillAdapter()

    complete = _service(tmp_path, FakeHistoryStore(quarter_row=_FakeRow(), annual_count=5), adapter, "c.json")
    assert complete._needs_backfill(company, 2026, 1, years=5) is False

    no_prev = _service(tmp_path, FakeHistoryStore(quarter_row=None, annual_count=5), adapter, "n.json")
    assert no_prev._needs_backfill(company, 2026, 1, years=5) is True

    too_few_annuals = _service(tmp_path, FakeHistoryStore(quarter_row=_FakeRow(), annual_count=2), adapter, "f.json")
    assert too_few_annuals._needs_backfill(company, 2026, 1, years=5) is True


def test_backfill_skips_companies_with_complete_history(monkeypatch, tmp_path):
    _patch_filing_context(
        monkeypatch,
        {"activeFinancialReport": {"fiscalYear": 2026, "quarter": 1}, "monthlyRevenuePeriod": "2026-03"},
    )
    companies = [_company("1111"), _company("2222")]
    service = _service(tmp_path, FakeHistoryStore(quarter_row=_FakeRow(), annual_count=5), FakeMopsBackfillAdapter())

    result = service.backfill(companies, limit=10, throttle_seconds=0, reset_progress=True)

    assert result.requestedCompanies == 0
    assert result.skippedCompanies == 2
    assert result.completed is True


def test_backfill_records_annual_period_failures(monkeypatch, tmp_path):
    _patch_filing_context(
        monkeypatch,
        {"activeFinancialReport": {"fiscalYear": 2026, "quarter": 1}, "monthlyRevenuePeriod": "2026-03"},
    )
    progress_path = tmp_path / "progress.json"
    service = OfficialHistoryBackfillService(
        history_store=OfficialFundamentalsHistoryStore(tmp_path / "history.json"),
        adapter=AllEmptyAdapter(),
        progress_store=BackfillProgressStore(progress_path),
    )

    result = service.backfill([_company("1111")], limit=1, throttle_seconds=0, reset_progress=True)

    assert result.failedCompanies == 1
    assert result.backfilledCompanies == 0
    assert result.incomeRows == 0
    assert "1111" in BackfillProgressStore(progress_path).load()["failedCompanies"]

    # A resume run skips the still-failing company instead of re-fetching it.
    resumed = service.backfill([_company("1111")], limit=1, throttle_seconds=0)
    assert resumed.requestedCompanies == 0
    assert resumed.skippedCompanies == 1


def test_backfill_skips_when_all_target_periods_present(monkeypatch, tmp_path):
    _patch_filing_context(
        monkeypatch,
        {"activeFinancialReport": {"fiscalYear": 2026, "quarter": 1}, "monthlyRevenuePeriod": "2026-03"},
    )
    # Company still "needs backfill" (only 2 annuals) but every requested period is
    # already populated, so there is nothing pending to fetch.
    service = _service(tmp_path, FakeHistoryStore(quarter_row=_FakeRow(), annual_count=2), FakeMopsBackfillAdapter())

    result = service.backfill([_company("1111")], limit=1, throttle_seconds=0, reset_progress=True)

    assert result.requestedCompanies == 0
    assert result.skippedCompanies == 1
    assert result.completed is True


def test_backfill_reconciles_stale_failures_on_resume(monkeypatch, tmp_path):
    _patch_filing_context(
        monkeypatch,
        {"activeFinancialReport": {"fiscalYear": 2026, "quarter": 1}, "monthlyRevenuePeriod": "2026-03"},
    )
    progress_path = tmp_path / "progress.json"
    seed = BackfillProgressStore(progress_path)
    # runKey must match what backfill() derives ("<year>Q<quarter>:<years>:<mode>").
    seed.save(
        {
            "schemaVersion": 1,
            "runKey": "2026Q1:5:strategy",
            "completedCompanies": [],
            # 1111: stale current-period "no rows" failure -> promoted to pending.
            # 2222: annual failure but history is now complete -> reconciled as done.
            "failedCompanies": {
                "1111": [{"period": "2026Q1", "error": "no official rows returned"}],
                "2222": [{"period": "2024Q4", "error": "no official rows returned"}],
            },
            "pendingCompanies": {},
        }
    )
    service = _service(
        tmp_path,
        FakeHistoryStore(quarter_row=_FakeRow(), annual_count=5),
        FakeMopsBackfillAdapter(),
    )

    result = service.backfill([_company("1111"), _company("2222")], limit=10, throttle_seconds=0)

    assert result.requestedCompanies == 0  # both reconciled, nothing re-fetched
    saved = BackfillProgressStore(progress_path).load()
    assert "1111" in saved["pendingCompanies"]
    assert saved["failedCompanies"] == {}


def test_backfill_derives_period_from_monthly_revenue_when_no_active_report(monkeypatch, tmp_path):
    _patch_filing_context(
        monkeypatch,
        {"activeFinancialReport": {}, "monthlyRevenuePeriod": "2026-05"},
    )
    service = _service(tmp_path, FakeHistoryStore(quarter_row=None, annual_count=0), FakeMopsBackfillAdapter())

    # 2026-05 -> fiscalYear 2026, quarter 2. throttle>0 exercises the inter-request sleep.
    result = service.backfill([_company("1111")], limit=1, throttle_seconds=0.001, reset_progress=True)

    assert "2026Q2" in result.periods
    assert result.progress["runKey"].startswith("2026Q2:")


def test_backfill_saves_progress_at_interval(monkeypatch, tmp_path):
    _patch_filing_context(
        monkeypatch,
        {"activeFinancialReport": {"fiscalYear": 2026, "quarter": 1}, "monthlyRevenuePeriod": "2026-03"},
    )
    companies = [_company(f"{1000 + i}") for i in range(11)]
    service = _service(tmp_path, FakeHistoryStore(quarter_row=None, annual_count=0), FakeMopsBackfillAdapter())

    result = service.backfill(companies, limit=11, throttle_seconds=0, reset_progress=True)

    assert result.requestedCompanies == 11
    assert result.completed is True


def test_backfill_main_runs_service_and_prints_json(monkeypatch, capsys):
    import json as _json
    import sys

    import backend.services.official_data_provider as odp
    import backend.services.official_history_backfill as bf

    class _FakeProvider:
        def __init__(self):
            self.history_store = object()

        def list_companies(self):
            return []

    class _FakeResult:
        def as_dict(self):
            return {"ok": True}

    class _FakeService:
        def __init__(self, store):
            self.store = store

        def backfill(self, companies, **kwargs):
            return _FakeResult()

    monkeypatch.setattr(odp, "OfficialDataProvider", _FakeProvider)
    monkeypatch.setattr(bf, "OfficialHistoryBackfillService", _FakeService)
    monkeypatch.setattr(sys, "argv", ["backfill", "--limit", "5", "--mode", "full_quarterly", "--reset-progress"])

    bf._main()

    assert _json.loads(capsys.readouterr().out) == {"ok": True}
