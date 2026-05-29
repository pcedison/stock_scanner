from backend.adapters.fundamentals_history import OfficialFundamentalsHistoryStore
from backend.adapters.mops_historical_fundamentals import OfficialMopsHistoricalFundamentalsAdapter
from backend.adapters.official_fundamentals import OfficialBalanceSheetRow, OfficialIncomeStatementRow
from backend.models.company import Company
from backend.services.official_history_backfill import (
    BackfillProgressStore,
    OfficialHistoryBackfillService,
    _full_quarterly_periods,
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


def test_backfill_service_treats_current_unannounced_period_as_pending(tmp_path):
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
