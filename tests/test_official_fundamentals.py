"""Parsing-focused tests for the official fundamentals adapter.

The adapter fetches from TWSE/TPEx OpenAPI over HTTP; these tests mock the
network layer (`_fetch_many` / `_fetch_json`) and assert the row-parsing
logic (ROC year/quarter conversion, market detection, margins, valuations),
which is the correctness-critical part.
"""

from datetime import date

import httpx

from backend.adapters.official_fundamentals import (
    OfficialFundamentalsAdapter,
    _gross_margin,
    _operating_margin,
    _pick,
    _quarter,
    _roc_year,
)


def test_roc_year_converts_民國_to_西元():
    assert _roc_year("113") == 2024
    assert _roc_year(113) == 2024
    assert _roc_year("") is None
    assert _roc_year(None) is None
    assert _roc_year("abc") is None


def test_quarter_only_accepts_1_to_4():
    assert _quarter("1") == 1
    assert _quarter(4) == 4
    assert _quarter("0") is None
    assert _quarter("5") is None
    assert _quarter("") is None
    assert _quarter("abc") is None


def test_margins_guard_none_and_zero():
    assert _gross_margin(1000.0, 400.0) == 40.0
    assert _gross_margin(0, 400.0) is None
    assert _gross_margin(None, 400.0) is None
    assert _gross_margin(1000.0, None) is None
    assert _operating_margin(1000.0, 300.0) == 30.0
    assert _operating_margin(0, 300.0) is None


def test_pick_returns_first_non_empty():
    row = {"a": "", "b": None, "c": "x"}
    assert _pick(row, "a", "b", "c") == "x"
    assert _pick(row, "a", "b") is None
    assert _pick(row, "missing") is None


def test_fetch_income_statements_parses_twse_and_tpex(monkeypatch):
    adapter = OfficialFundamentalsAdapter()
    twse_row = {
        "公司代號": "2330",
        "公司名稱": "台積電",
        "年度": "113",
        "季別": "1",
        "營業收入": "1,000",
        "營業成本": "600",
        "營業毛利（毛損）淨額": "400",
        "營業利益（損失）": "300",
        "本期淨利（淨損）": "250",
        "基本每股盈餘（元）": "9.5",
    }
    tpex_row = {
        "SecuritiesCompanyCode": "6488",
        "CompanyName": "環球晶",
        "Year": "113",
        "Season": "1",
        "收入": "500",
        "支出": "300",
        "營業毛利（毛損）": "200",
        "營業利益": "150",
        "稅後淨利": "120",
        "基本每股盈餘": "3.2",
    }
    skipped = {"公司代號": "", "公司名稱": "無代號"}
    non_digit = {"公司代號": "00尾盤", "公司名稱": "非數字"}
    monkeypatch.setattr(adapter, "_fetch_many", lambda urls: ([twse_row, tpex_row, skipped, non_digit], {"ok": True}))

    incomes, status = adapter.fetch_income_statements()

    assert set(incomes) == {"2330", "6488"}
    tsmc = incomes["2330"]
    assert tsmc.market == "TWSE"
    assert tsmc.fiscalYear == 2024
    assert tsmc.quarter == 1
    assert tsmc.revenue == 1000.0
    assert tsmc.costOfRevenue == 600.0
    assert tsmc.grossMargin == 40.0
    assert tsmc.operatingMargin == 30.0
    assert tsmc.netIncome == 250.0
    assert tsmc.eps == 9.5
    assert tsmc.source == "TWSE OpenAPI t187ap06"

    gw = incomes["6488"]
    assert gw.market == "TPEX"
    assert gw.revenue == 500.0
    assert gw.eps == 3.2
    assert gw.source == "TPEx OpenAPI mopsfin_t187ap06"
    assert status == {"ok": True}


def test_fetch_balance_sheets_picks_inventory(monkeypatch):
    adapter = OfficialFundamentalsAdapter()
    row = {"公司代號": "2330", "公司名稱": "台積電", "年度": "113", "季別": "1", "存貨": "1,234"}
    non_digit = {"公司代號": "00尾盤", "公司名稱": "非數字"}
    monkeypatch.setattr(adapter, "_fetch_many", lambda urls: ([row, non_digit], {"ok": True}))

    balances, _ = adapter.fetch_balance_sheets()

    assert set(balances) == {"2330"}  # non-digit code skipped
    assert balances["2330"].inventory == 1234.0
    assert balances["2330"].market == "TWSE"
    assert balances["2330"].source == "TWSE OpenAPI t187ap07"


def test_fetch_valuations_merges_twse_and_tpex(monkeypatch):
    adapter = OfficialFundamentalsAdapter()
    monkeypatch.setattr(adapter, "fetch_twse_valuations", lambda: ({"2330": object()}, {"ok": True}))
    monkeypatch.setattr(adapter, "fetch_tpex_valuations", lambda: ({"6488": object()}, {"ok": True}))

    valuations, status = adapter.fetch_valuations()

    assert set(valuations) == {"2330", "6488"}
    assert status == {"TWSE": {"ok": True}, "TPEX": {"ok": True}}


def test_fetch_twse_valuations_zips_fields_and_returns_first_ok_date(monkeypatch):
    adapter = OfficialFundamentalsAdapter()
    payload = {
        "stat": "OK",
        "date": "20260115",
        "fields": ["證券代號", "證券名稱", "殖利率(%)", "本益比", "股價淨值比", "財報年/季"],
        "data": [
            ["2330", "台積電", "2.5", "18.3", "5.1", "113/1"],
            ["", "無代號", "", "", "", ""],
        ],
    }
    calls = {"n": 0}

    def fake_fetch_json(url, params=None):
        calls["n"] += 1
        return payload

    monkeypatch.setattr(adapter, "_fetch_json", fake_fetch_json)

    valuations, status = adapter.fetch_twse_valuations(today=date(2026, 1, 15))

    assert calls["n"] == 1  # stops at the first OK date
    assert set(valuations) == {"2330"}
    row = valuations["2330"]
    assert row.per == 18.3
    assert row.priceBookRatio == 5.1
    assert row.dividendYield == 2.5
    assert row.fiscalQuarter == "113/1"
    assert status["2026-01-15"] == {"ok": True, "rows": 1}


def test_fetch_twse_valuations_all_dates_fail(monkeypatch):
    adapter = OfficialFundamentalsAdapter()
    monkeypatch.setattr(adapter, "_fetch_json", lambda url, params=None: {"stat": "very busy"})

    valuations, status = adapter.fetch_twse_valuations(today=date(2026, 1, 15))

    assert valuations == {}
    assert len(status) == 14  # tries 14 trailing days before giving up


def test_fetch_tpex_valuations_parses_list(monkeypatch):
    adapter = OfficialFundamentalsAdapter()
    rows = [
        {
            "SecuritiesCompanyCode": "6488",
            "CompanyName": "環球晶",
            "Date": "1150115",
            "PriceEarningRatio": "12.0",
            "PriceBookRatio": "2.0",
            "YieldRatio": "1.5",
        },
        {"SecuritiesCompanyCode": "", "CompanyName": "skip"},
    ]
    monkeypatch.setattr(adapter, "_fetch_json", lambda url, params=None: rows)

    valuations, status = adapter.fetch_tpex_valuations()

    assert set(valuations) == {"6488"}
    assert valuations["6488"].per == 12.0
    assert valuations["6488"].market == "TPEX"
    assert status["TPEX"] == {"ok": True, "rows": 1}


def test_fetch_twse_valuations_records_fetch_errors(monkeypatch):
    adapter = OfficialFundamentalsAdapter()

    def boom(url, params=None):
        raise RuntimeError("network down")

    monkeypatch.setattr(adapter, "_fetch_json", boom)

    valuations, status = adapter.fetch_twse_valuations(today=date(2026, 1, 15))

    assert valuations == {}
    assert len(status) == 14
    assert all(entry["ok"] is False for entry in status.values())
    assert any("network down" in str(entry.get("error")) for entry in status.values())


def test_fetch_tpex_valuations_handles_fetch_error(monkeypatch):
    adapter = OfficialFundamentalsAdapter()

    def boom(url, params=None):
        raise RuntimeError("tpex offline")

    monkeypatch.setattr(adapter, "_fetch_json", boom)

    valuations, status = adapter.fetch_tpex_valuations()

    assert valuations == {}
    assert status["TPEX"]["ok"] is False
    assert "tpex offline" in status["TPEX"]["error"]


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_fetch_json_returns_payload(monkeypatch):
    adapter = OfficialFundamentalsAdapter()
    captured: dict[str, object] = {}

    def fake_get(url, params=None, timeout=None, follow_redirects=None):
        captured.update(url=url, params=params, timeout=timeout, follow_redirects=follow_redirects)
        return _FakeResponse([{"公司代號": "2330"}])

    monkeypatch.setattr(httpx, "get", fake_get)

    assert adapter._fetch_json("http://x", params={"a": 1}) == [{"公司代號": "2330"}]
    assert captured == {"url": "http://x", "params": {"a": 1}, "timeout": adapter.timeout, "follow_redirects": True}


def test_fetch_with_retry_recovers_after_transient_failures(monkeypatch):
    adapter = OfficialFundamentalsAdapter()
    monkeypatch.setattr("backend.adapters.official_fundamentals.time.sleep", lambda _s: None)
    attempts = {"n": 0}

    def flaky(url):
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise httpx.RequestError("transient")
        return [{"ok": True}]

    monkeypatch.setattr(adapter, "_fetch_json", flaky)

    assert adapter._fetch_with_retry("http://x") == [{"ok": True}]
    assert attempts["n"] == 3  # two failures + one success


def test_fetch_with_retry_raises_last_error_after_exhausting_retries(monkeypatch):
    adapter = OfficialFundamentalsAdapter()
    monkeypatch.setattr("backend.adapters.official_fundamentals.time.sleep", lambda _s: None)

    def always_fail(url):
        raise httpx.RequestError("down")

    monkeypatch.setattr(adapter, "_fetch_json", always_fail)

    try:
        adapter._fetch_with_retry("http://x")
    except httpx.RequestError as exc:
        assert "down" in str(exc)
    else:
        raise AssertionError("expected the last error to propagate")


def test_fetch_many_degrades_when_one_source_fails(monkeypatch):
    adapter = OfficialFundamentalsAdapter()
    good, bad = "http://good", "http://bad"

    def per_url(url):
        if url == bad:
            raise httpx.RequestError("source offline")
        return [{"row": 1}, {"row": 2}]

    monkeypatch.setattr(adapter, "_fetch_with_retry", per_url)

    rows, status = adapter._fetch_many([good, bad])

    assert rows == [{"row": 1}, {"row": 2}]  # good source still contributes
    assert status[good] == {"ok": True, "rows": 2}
    assert status[bad]["ok"] is False
    assert "source offline" in status[bad]["error"]


def test_fetch_many_sequential_degrades_when_one_source_fails(monkeypatch):
    adapter = OfficialFundamentalsAdapter()
    good, bad = "http://good", "http://bad"

    def per_url(url, params=None):
        if url == bad:
            raise httpx.RequestError("source offline")
        return [{"row": 1}]

    monkeypatch.setattr(adapter, "_fetch_json", per_url)

    rows, status = adapter._fetch_many_sequential([good, bad])

    assert rows == [{"row": 1}]
    assert status[good] == {"ok": True, "rows": 1}
    assert status[bad]["ok"] is False
    assert "source offline" in status[bad]["error"]


def test_fetch_bundle_aggregates_counts(monkeypatch):
    adapter = OfficialFundamentalsAdapter()
    monkeypatch.setattr(adapter, "fetch_income_statements", lambda: ({"2330": object()}, {"i": 1}))
    monkeypatch.setattr(adapter, "fetch_balance_sheets", lambda: ({"2330": object(), "6488": object()}, {"b": 1}))
    monkeypatch.setattr(adapter, "fetch_valuations", lambda: ({"2330": object()}, {"v": 1}))

    bundle = adapter.fetch_bundle()

    assert bundle.status["incomeRows"] == 1
    assert bundle.status["balanceRows"] == 2
    assert bundle.status["valuationRows"] == 1
    assert bundle.status["incomeStatements"] == {"i": 1}
