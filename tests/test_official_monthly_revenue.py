"""Parsing tests for the official monthly-revenue / company-profile adapter.

This adapter is the source of the scan universe and revenue-YoY signals, so
its row parsing is correctness-critical. Network calls (`_fetch_json`) are
mocked; the parsing logic is asserted directly.
"""

from backend.adapters.official_monthly_revenue import (
    OfficialMonthlyRevenueAdapter,
    _to_int,
    roc_month_to_ad,
)


def test_roc_month_to_ad():
    assert roc_month_to_ad("11305") == "2024-05"
    assert roc_month_to_ad(11305) == "2024-05"
    assert roc_month_to_ad("11313") == ""  # month out of range
    assert roc_month_to_ad("1130") == ""  # too short
    assert roc_month_to_ad("") == ""
    assert roc_month_to_ad(None) == ""


def test_to_int_handles_commas_and_blanks():
    assert _to_int("1,234,567") == 1234567
    assert _to_int("12.0") == 12
    assert _to_int("-") is None
    assert _to_int("--") is None
    assert _to_int("") is None
    assert _to_int(None) is None
    assert _to_int("abc") is None


def test_fetch_monthly_revenue_parses_rows(monkeypatch):
    adapter = OfficialMonthlyRevenueAdapter()
    rows = [
        {
            "公司代號": "2330",
            "公司名稱": "台積電",
            "產業別": "半導體業",
            "出表日期": "1130610",
            "資料年月": "11305",
            "營業收入-當月營收": "1,000,000",
            "營業收入-去年同月增減(%)": "12.5",
            "累計營業收入-當月累計營收": "5,000,000",
            "累計營業收入-前期比較增減(%)": "8.0",
        },
        {"公司代號": "", "公司名稱": "無代號"},
        {"公司代號": "00X", "公司名稱": "非數字"},
    ]
    monkeypatch.setattr(adapter, "_fetch_json", lambda url: rows)

    parsed = adapter._fetch_monthly_revenue("https://example/twse", "TWSE")

    assert len(parsed) == 1
    row = parsed[0]
    assert row.stockCode == "2330"
    assert row.market == "TWSE"
    assert row.dataMonth == "2024-05"
    assert row.monthlyRevenue == 1000000
    assert row.monthlyRevenueYoY == 12.5
    assert row.cumulativeRevenue == 5000000
    assert row.cumulativeRevenueYoY == 8.0


def test_fetch_twse_company_profiles(monkeypatch):
    adapter = OfficialMonthlyRevenueAdapter()
    rows = [
        {"公司代號": "2330", "公司名稱": "台灣積體電路製造", "公司簡稱": "台積電", "產業別": "24", "出表日期": "1130610"},
        {"公司代號": "1101", "公司名稱": "台灣水泥", "產業別": "01", "出表日期": "1130610"},
    ]
    monkeypatch.setattr(adapter, "_fetch_json", lambda url: rows)

    profiles = adapter.fetch_twse_company_profiles()

    assert [p.stockCode for p in profiles] == ["2330", "1101"]
    assert profiles[0].companyShortName == "台積電"
    # falls back to full name when 公司簡稱 is missing
    assert profiles[1].companyShortName == "台灣水泥"
    assert profiles[0].market == "TWSE"
    assert profiles[0].industryCode == "24"


def test_fetch_tpex_company_profiles(monkeypatch):
    adapter = OfficialMonthlyRevenueAdapter()
    rows = [
        {
            "SecuritiesCompanyCode": "6488",
            "CompanyName": "環球晶圓",
            "CompanyAbbreviation": "環球晶",
            "SecuritiesIndustryCode": "24",
            "Date": "1130610",
        }
    ]
    monkeypatch.setattr(adapter, "_fetch_json", lambda url: rows)

    profiles = adapter.fetch_tpex_company_profiles()

    assert profiles[0].stockCode == "6488"
    assert profiles[0].companyShortName == "環球晶"
    assert profiles[0].market == "TPEX"
    assert profiles[0].industryCode == "24"


def test_fetch_monthly_revenue_combines_both_markets(monkeypatch):
    adapter = OfficialMonthlyRevenueAdapter()
    monkeypatch.setattr(adapter, "fetch_twse_monthly_revenue", lambda: ["twse-a", "twse-b"])
    monkeypatch.setattr(adapter, "fetch_tpex_monthly_revenue", lambda: ["tpex-a"])

    assert adapter.fetch_monthly_revenue() == ["twse-a", "twse-b", "tpex-a"]


def test_fetch_company_profiles_combines_both_markets(monkeypatch):
    adapter = OfficialMonthlyRevenueAdapter()
    monkeypatch.setattr(adapter, "fetch_twse_company_profiles", lambda: ["twse"])
    monkeypatch.setattr(adapter, "fetch_tpex_company_profiles", lambda: ["tpex-a", "tpex-b"])

    assert adapter.fetch_company_profiles() == ["twse", "tpex-a", "tpex-b"]
    assert adapter.profile_fallbacks == {}


def _revenue_row(code, market):
    from backend.adapters.official_monthly_revenue import OfficialMonthlyRevenueRow

    return OfficialMonthlyRevenueRow(
        stockCode=code,
        companyName=f"name-{code}",
        market=market,
        industryName="半導體業",
        reportDate="1150910",
        dataMonth="2026-08",
        monthlyRevenue=1,
        monthlyRevenueYoY=1.0,
        cumulativeRevenue=1,
        cumulativeRevenueYoY=1.0,
    )


def test_fetch_company_profiles_derives_failed_market_from_monthly_revenue(monkeypatch):
    # 2026-09-13: the TPEX profile endpoint reset every connection mid-body for hours,
    # which zeroed the whole universe and blocked every R2 rebuild.
    adapter = OfficialMonthlyRevenueAdapter()

    def broken():
        raise RuntimeError("Failed to fetch official endpoint after 3 attempts: tpex profiles")

    monkeypatch.setattr(adapter, "fetch_twse_company_profiles", lambda: ["twse"])
    monkeypatch.setattr(adapter, "fetch_tpex_company_profiles", broken)
    monkeypatch.setattr(
        adapter, "fetch_tpex_monthly_revenue", lambda: [_revenue_row("6488", "TPEX"), _revenue_row("abc", "TPEX")]
    )

    profiles = adapter.fetch_company_profiles()

    assert profiles[0] == "twse"
    derived = profiles[1:]
    assert [row.stockCode for row in derived] == ["6488"]
    assert derived[0].market == "TPEX"
    assert derived[0].companyShortName == "name-6488"
    assert derived[0].industryName == "半導體業"
    assert adapter.profile_fallbacks == {"TPEX": "monthly_revenue"}


def test_fetch_company_profiles_raises_when_fallback_also_fails(monkeypatch):
    import pytest

    adapter = OfficialMonthlyRevenueAdapter()

    def broken():
        raise RuntimeError("profiles down")

    def revenue_broken():
        raise RuntimeError("revenue down")

    monkeypatch.setattr(adapter, "fetch_twse_company_profiles", lambda: ["twse"])
    monkeypatch.setattr(adapter, "fetch_tpex_company_profiles", broken)
    monkeypatch.setattr(adapter, "fetch_tpex_monthly_revenue", revenue_broken)

    with pytest.raises(RuntimeError, match="profiles down"):
        adapter.fetch_company_profiles()
