import json

from backend.adapters.fundamentals_history import OfficialFundamentalsHistoryStore
from backend.adapters.fundamentals_import import (
    ImportedAnnualFinancial,
    ImportedFundamentalBundle,
    ImportedMonthlyRevenueMetrics,
    ImportedQuarterlyFundamental,
    ImportedValuation,
    LocalFundamentalsImportAdapter,
)
from backend.adapters.monthly_revenue_history import MonthlyRevenueHistoryStore
from backend.adapters.official_fundamentals import (
    OfficialBalanceSheetRow,
    OfficialFundamentalBundle,
    OfficialIncomeStatementRow,
    OfficialValuationRow,
)
from backend.adapters.official_monthly_revenue import (
    OfficialCompanyProfileRow,
    OfficialMonthlyRevenueRow,
    roc_month_to_ad,
)
from backend.models.settings import ScannerSettings
from backend.services.official_data_provider import (
    OfficialDataProvider,
    _first_present,
    _is_financial_company,
    _latest_completed_quarter_from_month,
)
from backend.services.rules import RuleEngine


class FakeOfficialAdapter:
    def fetch_company_profiles(self):
        return [
            OfficialCompanyProfileRow(
                stockCode="9999",
                companyName="測試科技股份有限公司",
                companyShortName="測試科技",
                market="TWSE",
                industryName="半導體業",
                industryCode="24",
                reportDate="1150512",
            ),
            OfficialCompanyProfileRow(
                stockCode="2888",
                companyName="測試金控股份有限公司",
                companyShortName="測試金",
                market="TWSE",
                industryName="金融保險業",
                industryCode="17",
                reportDate="1150512",
            ),
        ]

    def fetch_monthly_revenue(self):
        return [
            OfficialMonthlyRevenueRow(
                stockCode="9999",
                companyName="測試科技",
                market="TWSE",
                industryName="半導體業",
                reportDate="1150512",
                dataMonth="2026-04",
                monthlyRevenue=100000,
                monthlyRevenueYoY=66.6,
                cumulativeRevenue=400000,
                cumulativeRevenueYoY=55.5,
            )
        ]


class FakeFundamentalsAdapter:
    def fetch_bundle(self):
        return OfficialFundamentalBundle(
            incomes={
                "9999": OfficialIncomeStatementRow(
                    stockCode="9999",
                    companyName="測試科技",
                    market="TWSE",
                    fiscalYear=2026,
                    quarter=1,
                    revenue=100000.0,
                    costOfRevenue=60000.0,
                    grossProfit=40000.0,
                    grossMargin=40.0,
                    operatingIncome=20000.0,
                    operatingMargin=20.0,
                    netIncome=15000.0,
                    eps=2.5,
                    source="fake income",
                )
            },
            balances={
                "9999": OfficialBalanceSheetRow(
                    stockCode="9999",
                    companyName="測試科技",
                    market="TWSE",
                    fiscalYear=2026,
                    quarter=1,
                    inventory=None,
                    source="fake balance",
                )
            },
            valuations={
                "9999": OfficialValuationRow(
                    stockCode="9999",
                    companyName="測試科技",
                    market="TWSE",
                    date="20260513",
                    per=12.3,
                    priceBookRatio=1.8,
                    dividendYield=2.1,
                    fiscalQuarter="115/1",
                    source="fake valuation",
                )
            },
            status={"incomeRows": 1, "balanceRows": 1, "valuationRows": 1},
        )


class ExplodingFundamentalsAdapter:
    def fetch_bundle(self):
        raise AssertionError("company search must not fetch full fundamentals")


class FakeEmptyImportAdapter:
    def fetch_bundle(self):
        return ImportedFundamentalBundle(status={"rows": 0})


class ExplodingMonthlyRevenueHistory:
    path = "monthly_revenue_history.json"

    def merge_rows(self, rows):
        raise OSError("history store is read-only")

    def previous_month_yoy(self, stock_code, current_month):
        return None

    def trailing_three_month_avg_yoy(self, stock_code, current_month):
        return None

    def jan_feb_combined_yoy(self, stock_code, current_year):
        return None


class FakeFundamentalsAdapterWithInventory(FakeFundamentalsAdapter):
    def fetch_bundle(self):
        bundle = super().fetch_bundle()
        bundle.balances["9999"] = OfficialBalanceSheetRow(
            stockCode="9999",
            companyName="測試科技",
            market="TWSE",
            fiscalYear=2026,
            quarter=1,
            inventory=120000.0,
            source="fake balance",
        )
        return bundle


class FakeImportAdapter:
    def fetch_bundle(self):
        return ImportedFundamentalBundle(
            monthly={
                "9999": ImportedMonthlyRevenueMetrics(
                    stockCode="9999",
                    previousMonthRevenueYoY=70.0,
                    trailingThreeMonthAverageYoY=64.0,
                    janFebCombinedRevenueYoY=61.0,
                )
            },
            quarterly={
                "9999": ImportedQuarterlyFundamental(
                    stockCode="9999",
                    fiscalYear=2026,
                    quarter=1,
                    eps=3.0,
                    epsYoY=25.0,
                    netIncome=18000.0,
                    netIncomeYoY=30.0,
                    grossMarginYoY=4.0,
                )
            },
            valuations={
                "9999": ImportedValuation(
                    stockCode="9999",
                    per=11.0,
                    inventoryTurnover=4.2,
                )
            },
            annuals={
                "9999": [
                    ImportedAnnualFinancial(stockCode="9999", year=2023, netIncome=1000.0),
                    ImportedAnnualFinancial(stockCode="9999", year=2024, netIncome=1200.0),
                    ImportedAnnualFinancial(stockCode="9999", year=2025, netIncome=1500.0),
                ]
            },
            status={"rows": 6},
        )


def test_roc_month_to_ad_converts_official_month():
    assert roc_month_to_ad("11504") == "2026-04"


def test_official_provider_builds_universe_and_marks_missing_fundamentals(tmp_path):
    provider = OfficialDataProvider(
        adapter=FakeOfficialAdapter(),
        fundamentals_adapter=FakeFundamentalsAdapter(),
        import_adapter=FakeEmptyImportAdapter(),
        history_store=OfficialFundamentalsHistoryStore(tmp_path / "history.json"),
        monthly_revenue_history=MonthlyRevenueHistoryStore(tmp_path / "monthly_revenue_history.json"),
    )

    companies = provider.list_companies()
    snapshot = provider.get_snapshot("9999")
    result = RuleEngine().evaluate_entry(snapshot, ScannerSettings(use_mock_data=False))

    assert len(companies) == 2
    assert provider.status()["monthlySnapshots"] == 1
    assert snapshot.company.name == "測試科技"
    assert snapshot.quarterlyFinancial.eps == 2.5
    assert snapshot.quarterlyFinancial.netIncome == 15000.0
    assert snapshot.valuation.per == 12.3
    assert result.status == "INSUFFICIENT_DATA"
    assert {reason.code for reason in result.reasons} >= {"E1", "E2", "E4", "E5", "OFFICIAL_Q", "OFFICIAL_VALUATION"}


def test_official_provider_search_uses_lightweight_company_profiles(tmp_path):
    provider = OfficialDataProvider(
        adapter=FakeOfficialAdapter(),
        fundamentals_adapter=ExplodingFundamentalsAdapter(),
        import_adapter=FakeEmptyImportAdapter(),
        history_store=OfficialFundamentalsHistoryStore(tmp_path / "history.json"),
        monthly_revenue_history=MonthlyRevenueHistoryStore(tmp_path / "monthly_revenue_history.json"),
    )

    matches = provider.search_companies("9999")
    status = provider.status()

    assert [company.stockCode for company in matches] == ["9999"]
    assert status["companies"] == 2
    assert status["monthlySnapshots"] == 0


def test_official_provider_merges_csv_import_fundamentals(tmp_path):
    provider = OfficialDataProvider(
        adapter=FakeOfficialAdapter(),
        fundamentals_adapter=FakeFundamentalsAdapter(),
        import_adapter=FakeImportAdapter(),
        history_store=OfficialFundamentalsHistoryStore(tmp_path / "history.json"),
        monthly_revenue_history=MonthlyRevenueHistoryStore(tmp_path / "monthly_revenue_history.json"),
    )

    snapshot = provider.get_snapshot("9999")

    assert snapshot.monthlyRevenue.previousMonthRevenueYoY == 70.0
    assert snapshot.monthlyRevenue.trailingThreeMonthAverageYoY == 64.0
    assert snapshot.quarterlyFinancial.quarter == "2026Q1"
    assert snapshot.quarterlyFinancial.eps == 3.0
    assert snapshot.quarterlyFinancial.epsYoY == 25.0
    assert snapshot.quarterlyFinancial.netIncomeYoY == 30.0
    assert snapshot.quarterlyFinancial.grossMargin == 40.0
    assert snapshot.quarterlyFinancial.grossMarginYoY == 4.0
    assert snapshot.valuation.per == 11.0
    assert snapshot.valuation.inventoryTurnover == 4.2
    assert [row.year for row in snapshot.annualFinancials] == [2023, 2024, 2025]
    assert provider.status()["sourceStatus"]["fundamentalsImport"]["rows"] == 6


def test_official_provider_surfaces_monthly_history_persistence_errors(tmp_path):
    provider = OfficialDataProvider(
        adapter=FakeOfficialAdapter(),
        fundamentals_adapter=FakeFundamentalsAdapter(),
        import_adapter=FakeEmptyImportAdapter(),
        history_store=OfficialFundamentalsHistoryStore(tmp_path / "history.json"),
        monthly_revenue_history=ExplodingMonthlyRevenueHistory(),
    )

    snapshot = provider.get_snapshot("9999")
    status = provider.status()["sourceStatus"]["monthlyRevenueHistory"]

    assert snapshot.company.stockCode == "9999"
    assert status["enabled"] is False
    assert status["hasError"] is True
    assert "error" not in status


def test_official_provider_uses_official_history_for_yoy_annuals_and_inventory(tmp_path):
    history_path = tmp_path / "history.json"
    history_path.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "updatedAt": "2026-05-13T00:00:00+00:00",
                "quarters": {
                    "9999": {
                        "2021Q4": {"stockCode": "9999", "period": "2021Q4", "fiscalYear": 2021, "quarter": 4, "netIncome": 1000.0},
                        "2022Q4": {"stockCode": "9999", "period": "2022Q4", "fiscalYear": 2022, "quarter": 4, "netIncome": 1200.0},
                        "2023Q4": {"stockCode": "9999", "period": "2023Q4", "fiscalYear": 2023, "quarter": 4, "netIncome": 1500.0},
                        "2024Q4": {"stockCode": "9999", "period": "2024Q4", "fiscalYear": 2024, "quarter": 4, "netIncome": 1800.0},
                        "2025Q1": {
                            "stockCode": "9999",
                            "period": "2025Q1",
                            "fiscalYear": 2025,
                            "quarter": 1,
                            "eps": 2.0,
                            "netIncome": 10000.0,
                            "grossMargin": 35.0,
                        },
                        "2025Q4": {"stockCode": "9999", "period": "2025Q4", "fiscalYear": 2025, "quarter": 4, "netIncome": 2400.0},
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    provider = OfficialDataProvider(
        adapter=FakeOfficialAdapter(),
        fundamentals_adapter=FakeFundamentalsAdapterWithInventory(),
        import_adapter=FakeEmptyImportAdapter(),
        history_store=OfficialFundamentalsHistoryStore(history_path),
        monthly_revenue_history=MonthlyRevenueHistoryStore(tmp_path / "monthly_revenue_history.json"),
    )

    snapshot = provider.get_snapshot("9999")
    status = provider.status()["sourceStatus"]["officialFundamentalsHistory"]

    assert snapshot.quarterlyFinancial.quarter == "2026Q1"
    assert snapshot.quarterlyFinancial.epsYoY == 25.0
    assert snapshot.quarterlyFinancial.netIncomeYoY == 50.0
    assert snapshot.quarterlyFinancial.grossMarginYoY == 5.0
    assert snapshot.valuation.inventoryTurnover == 2.0
    assert [row.year for row in snapshot.annualFinancials] == [2021, 2022, 2023, 2024, 2025]
    assert status["updatedRows"] >= 1


def test_local_fundamentals_import_adapter_parses_csv(tmp_path):
    path = tmp_path / "fundamentals_import.csv"
    path.write_text(
        "\n".join(
            [
                "stock_code,month,monthly_revenue_yoy,previous_month_revenue_yoy,cumulative_revenue_yoy,trailing_3m_average_yoy,jan_feb_combined_revenue_yoy,is_spring_festival_month,fiscal_year,quarter,eps,eps_yoy,net_income,net_income_yoy,revenue,gross_margin,gross_margin_yoy,operating_margin,per,price_book_ratio,dividend_yield,valuation_date,valuation_fiscal_quarter,inventory_turnover,annual_year,annual_net_income",
                "9999,2026-04,66.6,61.2,55.5,60.1,58.8,false,2026,1,3.1,24.5,15000,31.2,100000,40.0,2.2,20.0,11.7,1.6,2.4,20260513,2026Q1,3.8,2025,9000",
            ]
        ),
        encoding="utf-8",
    )

    bundle = LocalFundamentalsImportAdapter(path).fetch_bundle()

    assert bundle.status["rows"] == 1
    assert bundle.monthly["9999"].previousMonthRevenueYoY == 61.2
    assert bundle.quarterly["9999"].epsYoY == 24.5
    assert bundle.valuations["9999"].inventoryTurnover == 3.8
    assert bundle.annuals["9999"][0].year == 2025


# --------------------------------------------------------------------------- #
# Module-level helper branches
# --------------------------------------------------------------------------- #


def test_is_financial_company_keyword_range_and_non_numeric():
    assert _is_financial_company("9999", "金融保險業", "某科技") is True  # keyword
    assert _is_financial_company("2850", "產物保險", "某產險") is True  # keyword wins
    assert _is_financial_company("2880", "Unknown", "Plain Co") is True  # 2801-2999 range
    assert _is_financial_company("1101", "水泥工業", "某水泥") is False  # out of range
    assert _is_financial_company("ABCD", "電子工業", "某電子") is False  # non-numeric code


def test_latest_completed_quarter_from_month_branches():
    assert _latest_completed_quarter_from_month("2026-04") == "2026Q1"
    assert _latest_completed_quarter_from_month("2026-02") == "2025Q4"  # Q1 month rolls back a year
    assert _latest_completed_quarter_from_month("garbage") == "0000Q0"


def test_first_present_returns_first_non_none():
    assert _first_present(None, None, 3, 4) == 3
    assert _first_present(None, None) is None


# --------------------------------------------------------------------------- #
# Provider branch coverage
# --------------------------------------------------------------------------- #


def _provider(tmp_path, **overrides):
    kwargs = {
        "adapter": FakeOfficialAdapter(),
        "fundamentals_adapter": FakeFundamentalsAdapter(),
        "import_adapter": FakeEmptyImportAdapter(),
        "history_store": OfficialFundamentalsHistoryStore(tmp_path / "history.json"),
        "monthly_revenue_history": MonthlyRevenueHistoryStore(tmp_path / "monthly_revenue_history.json"),
    }
    kwargs.update(overrides)
    return OfficialDataProvider(**kwargs)


def test_company_from_profile_rejects_blank_or_non_numeric_code(tmp_path):
    provider = _provider(tmp_path)
    blank = OfficialCompanyProfileRow(
        stockCode="",
        companyName="無代號",
        companyShortName="無代號",
        market="TWSE",
        industryName="其他",
        industryCode="99",
        reportDate="1150512",
    )
    assert provider._company_from_profile(blank) is None


class CountingAdapter(FakeOfficialAdapter):
    def __init__(self):
        self.profile_calls = 0

    def fetch_company_profiles(self):
        self.profile_calls += 1
        return super().fetch_company_profiles()


def test_refresh_companies_skips_when_cache_is_fresh(tmp_path):
    adapter = CountingAdapter()
    provider = _provider(tmp_path, adapter=adapter)
    provider.refresh_companies()
    provider.refresh_companies()  # cache still fresh -> outer guard returns early
    assert adapter.profile_calls == 1


def test_refresh_companies_inner_guard_short_circuits(tmp_path, monkeypatch):
    adapter = CountingAdapter()
    provider = _provider(tmp_path, adapter=adapter)
    checks = iter([True, False])  # outer sees stale, inner (post-lock) sees fresh
    monkeypatch.setattr(provider, "_needs_profiles_refresh", lambda: next(checks))
    provider.refresh_companies()
    assert adapter.profile_calls == 0  # bailed at the double-checked guard before fetching


def test_refresh_snapshots_skips_when_cache_is_fresh(tmp_path):
    provider = _provider(tmp_path)
    provider.refresh()
    calls = {"n": 0}
    original = provider.adapter.fetch_company_profiles

    def counting():
        calls["n"] += 1
        return original()

    provider.adapter.fetch_company_profiles = counting
    provider.refresh()  # cache fresh -> outer guard returns early, no refetch
    assert calls["n"] == 0


def test_refresh_snapshots_inner_guard_short_circuits(tmp_path, monkeypatch):
    provider = _provider(tmp_path)
    checks = iter([True, False])
    monkeypatch.setattr(provider, "_needs_snapshots_refresh", lambda: next(checks))
    provider.refresh()
    assert provider._snapshots == {}  # inner guard bailed before building snapshots


class AdapterWithBlankProfile(FakeOfficialAdapter):
    def fetch_company_profiles(self):
        profiles = super().fetch_company_profiles()
        profiles.append(
            OfficialCompanyProfileRow(
                stockCode="",
                companyName="無代號",
                companyShortName="無代號",
                market="TWSE",
                industryName="其他",
                industryCode="99",
                reportDate="1150512",
            )
        )
        return profiles


def test_refresh_skips_profiles_without_valid_code(tmp_path):
    provider = _provider(tmp_path, adapter=AdapterWithBlankProfile())
    provider.get_snapshot("9999")  # full refresh() path, where the blank profile is skipped
    assert [c.stockCode for c in provider.list_companies()] == ["2888", "9999"]


class FakeFundamentalsAdapterNoIncome:
    def fetch_bundle(self):
        return OfficialFundamentalBundle(incomes={}, balances={}, valuations={}, status={})


def test_refresh_falls_back_to_history_latest_quarter(tmp_path):
    history_path = tmp_path / "history.json"
    history_path.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "updatedAt": "2026-05-13T00:00:00+00:00",
                "quarters": {
                    "9999": {
                        "2025Q4": {"stockCode": "9999", "period": "2025Q4", "fiscalYear": 2025, "quarter": 4, "netIncome": 2400.0},
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    provider = _provider(
        tmp_path,
        fundamentals_adapter=FakeFundamentalsAdapterNoIncome(),
        history_store=OfficialFundamentalsHistoryStore(history_path),
    )
    snapshot = provider.get_snapshot("9999")
    # No income and no exact-match history for the month-derived 2026Q1 ->
    # falls back to the latest stored quarter.
    assert snapshot.quarterlyFinancial.quarter == "2025Q4"


class FakeFundamentalsAdapterQ4(FakeFundamentalsAdapter):
    def fetch_bundle(self):
        bundle = super().fetch_bundle()
        bundle.incomes["9999"] = OfficialIncomeStatementRow(
            stockCode="9999",
            companyName="測試科技",
            market="TWSE",
            fiscalYear=2025,
            quarter=4,
            revenue=100000.0,
            costOfRevenue=60000.0,
            grossProfit=40000.0,
            grossMargin=40.0,
            operatingIncome=20000.0,
            operatingMargin=20.0,
            netIncome=99000.0,
            eps=9.9,
            source="fake income q4",
        )
        return bundle


def test_refresh_appends_annual_from_q4_income(tmp_path):
    provider = _provider(tmp_path, fundamentals_adapter=FakeFundamentalsAdapterQ4())
    snapshot = provider.get_snapshot("9999")
    annual_2025 = [row for row in snapshot.annualFinancials if row.year == 2025]
    assert annual_2025 and annual_2025[0].netIncome == 99000.0


class FakeImportAdapterBadMonth:
    def fetch_bundle(self):
        return ImportedFundamentalBundle(
            monthly={"9999": ImportedMonthlyRevenueMetrics(stockCode="9999", month="abcd-04")},
            status={"rows": 1},
        )


def test_refresh_handles_unparseable_snapshot_month_year(tmp_path):
    provider = _provider(tmp_path, import_adapter=FakeImportAdapterBadMonth())
    snapshot = provider.get_snapshot("9999")
    # Non-numeric year -> snapshot_year falls back to 0, so the Jan/Feb combo is skipped.
    assert snapshot.monthlyRevenue.month == "abcd-04"
    assert snapshot.monthlyRevenue.janFebCombinedRevenueYoY is None


def test_search_companies_empty_query_returns_empty(tmp_path):
    provider = _provider(tmp_path)
    assert provider.search_companies("   ") == []


def test_search_companies_prefix_and_substring_branches(tmp_path):
    provider = _provider(tmp_path)
    provider.list_companies()
    assert [c.stockCode for c in provider.search_companies("99")] == ["9999"]  # code prefix
    assert "9999" in {c.stockCode for c in provider.search_companies("半導")}  # industry substring


def test_get_company_hit_and_miss(tmp_path):
    provider = _provider(tmp_path)
    assert provider.get_company("9999").stockCode == "9999"
    assert provider.get_company("0000") is None


class AdapterWithTpex(FakeOfficialAdapter):
    def fetch_company_profiles(self):
        profiles = super().fetch_company_profiles()
        profiles.append(
            OfficialCompanyProfileRow(
                stockCode="6666",
                companyName="測試上櫃",
                companyShortName="測試櫃",
                market="TPEX",
                industryName="電子業",
                industryCode="24",
                reportDate="1150512",
            )
        )
        return profiles

    def fetch_monthly_revenue(self):
        rows = super().fetch_monthly_revenue()
        rows.append(
            OfficialMonthlyRevenueRow(
                stockCode="6666",
                companyName="測試上櫃",
                market="TPEX",
                industryName="電子業",
                reportDate="1150512",
                dataMonth="2026-04",
                monthlyRevenue=50000,
                monthlyRevenueYoY=10.0,
                cumulativeRevenue=200000,
                cumulativeRevenueYoY=8.0,
            )
        )
        return rows


def test_list_snapshots_respects_market_toggles(tmp_path):
    provider = _provider(tmp_path, adapter=AdapterWithTpex())
    twse_only = provider.list_snapshots(ScannerSettings(use_mock_data=False, scan_tpex=False))
    tpex_only = provider.list_snapshots(ScannerSettings(use_mock_data=False, scan_twse=False))
    assert {s.company.market for s in twse_only} == {"TWSE"}
    assert {s.company.market for s in tpex_only} == {"TPEX"}


def test_status_refresh_true_and_cold_company_load(tmp_path):
    provider = _provider(tmp_path)
    cold = provider.status()  # no companies yet -> triggers a company refresh
    assert cold["companies"] == 2
    warm = provider.status(refresh=True)  # refresh=True -> full snapshot refresh
    assert warm["monthlySnapshots"] == 1
