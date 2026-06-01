"""Network-orchestration + degradation tests for the MOPS historical adapter.

The parse_* methods are covered in test_mops_historical_fundamentals.py with
HTML/JSON fixtures. These tests cover the surrounding fetch layer — the JSON
API → HTML fallback → empty cascade and the HTTP/error degradation paths — so
a MOPS outage or format change degrades gracefully instead of crashing a scan.
"""

import httpx

from backend.adapters.mops_historical_fundamentals import (
    OfficialMopsHistoricalFundamentalsAdapter,
)


class _FakeResponse:
    def __init__(self, *, json_body=None, text="", raise_exc=None):
        self._json_body = json_body
        self.text = text
        self._raise_exc = raise_exc
        self.encoding = None

    def raise_for_status(self):
        if self._raise_exc is not None:
            raise self._raise_exc

    def json(self):
        if self._json_body is None:
            raise ValueError("no json body")
        return self._json_body


# --- _fetch_statement_payload (JSON API) ------------------------------------


def test_fetch_statement_payload_returns_result_dict(monkeypatch):
    adapter = OfficialMopsHistoricalFundamentalsAdapter()
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *a, **k: _FakeResponse(json_body={"code": 200, "result": {"reportList": [["x"]]}}),
    )

    result = adapter._fetch_statement_payload("http://api", "2330", 2024, 1)

    assert result == {"reportList": [["x"]]}


def test_fetch_statement_payload_returns_none_on_http_error(monkeypatch):
    adapter = OfficialMopsHistoricalFundamentalsAdapter()
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *a, **k: _FakeResponse(raise_exc=httpx.HTTPError("boom")),
    )

    assert adapter._fetch_statement_payload("http://api", "2330", 2024, 1) is None


def test_fetch_statement_payload_returns_none_on_non_200_code(monkeypatch):
    adapter = OfficialMopsHistoricalFundamentalsAdapter()
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *a, **k: _FakeResponse(json_body={"code": 900, "result": {"x": 1}}),
    )

    assert adapter._fetch_statement_payload("http://api", "2330", 2024, 1) is None


def test_fetch_statement_payload_returns_none_when_result_not_dict(monkeypatch):
    adapter = OfficialMopsHistoricalFundamentalsAdapter()
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *a, **k: _FakeResponse(json_body={"code": 200, "result": "not-a-dict"}),
    )

    assert adapter._fetch_statement_payload("http://api", "2330", 2024, 1) is None


# --- _fetch_statement (HTML fallback) ---------------------------------------


def test_fetch_statement_returns_text_on_success(monkeypatch):
    adapter = OfficialMopsHistoricalFundamentalsAdapter()
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResponse(text="<table>data</table>"))

    assert adapter._fetch_statement("http://html", "2330", "TWSE", 2024, 1) == "<table>data</table>"


def test_fetch_statement_returns_none_on_http_error(monkeypatch):
    adapter = OfficialMopsHistoricalFundamentalsAdapter()
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResponse(raise_exc=httpx.HTTPError("boom")))

    assert adapter._fetch_statement("http://html", "2330", "TWSE", 2024, 1) is None


def test_fetch_statement_returns_none_on_no_data_sentinel(monkeypatch):
    adapter = OfficialMopsHistoricalFundamentalsAdapter()
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResponse(text="查無資料"))

    assert adapter._fetch_statement("http://html", "2330", "TWSE", 2024, 1) is None


# --- _fetch_income / _fetch_balance cascade ---------------------------------


def test_fetch_income_uses_json_payload_when_available(monkeypatch):
    adapter = OfficialMopsHistoricalFundamentalsAdapter()
    monkeypatch.setattr(adapter, "_fetch_statement_payload", lambda *a: {"reportList": []})
    monkeypatch.setattr(adapter, "parse_income_payload", lambda *a: ["row"])

    rows, status = adapter._fetch_income("2330", "台積電", "TWSE", 2024, 1)

    assert rows == ["row"]
    assert status == {"ok": True, "rows": 1}


def test_fetch_income_falls_back_to_html_when_no_payload(monkeypatch):
    adapter = OfficialMopsHistoricalFundamentalsAdapter()
    monkeypatch.setattr(adapter, "_fetch_statement_payload", lambda *a: None)
    monkeypatch.setattr(adapter, "_fetch_statement", lambda *a: "<table></table>")
    monkeypatch.setattr(adapter, "parse_income_statement", lambda *a: ["html-row"])

    rows, status = adapter._fetch_income("2330", "台積電", "TWSE", 2024, 1)

    assert rows == ["html-row"]
    assert status == {"ok": True, "rows": 1, "fallback": "html"}


def test_fetch_income_returns_empty_when_both_sources_fail(monkeypatch):
    adapter = OfficialMopsHistoricalFundamentalsAdapter()
    monkeypatch.setattr(adapter, "_fetch_statement_payload", lambda *a: None)
    monkeypatch.setattr(adapter, "_fetch_statement", lambda *a: None)

    rows, status = adapter._fetch_income("2330", "台積電", "TWSE", 2024, 1)

    assert rows == []
    assert status == {"ok": False, "rows": 0}


def test_fetch_balance_uses_json_payload_when_available(monkeypatch):
    adapter = OfficialMopsHistoricalFundamentalsAdapter()
    monkeypatch.setattr(adapter, "_fetch_statement_payload", lambda *a: {"reportList": []})
    monkeypatch.setattr(adapter, "parse_balance_payload", lambda *a: ["bal"])

    rows, status = adapter._fetch_balance("2330", "台積電", "TWSE", 2024, 1)

    assert rows == ["bal"]
    assert status == {"ok": True, "rows": 1}


def test_fetch_balance_falls_back_to_html_when_no_payload(monkeypatch):
    adapter = OfficialMopsHistoricalFundamentalsAdapter()
    monkeypatch.setattr(adapter, "_fetch_statement_payload", lambda *a: None)
    monkeypatch.setattr(adapter, "_fetch_statement", lambda *a: "<table></table>")
    monkeypatch.setattr(adapter, "parse_balance_sheet", lambda *a: ["bal-row"])

    rows, status = adapter._fetch_balance("2330", "台積電", "TWSE", 2024, 1)

    assert rows == ["bal-row"]
    assert status == {"ok": True, "rows": 1, "fallback": "html"}


def test_fetch_balance_returns_empty_when_both_sources_fail(monkeypatch):
    adapter = OfficialMopsHistoricalFundamentalsAdapter()
    monkeypatch.setattr(adapter, "_fetch_statement_payload", lambda *a: None)
    monkeypatch.setattr(adapter, "_fetch_statement", lambda *a: None)

    rows, status = adapter._fetch_balance("2330", "台積電", "TWSE", 2024, 1)

    assert rows == []
    assert status == {"ok": False, "rows": 0}


def test_fetch_company_period_combines_income_and_balance(monkeypatch):
    adapter = OfficialMopsHistoricalFundamentalsAdapter()
    monkeypatch.setattr(adapter, "_fetch_income", lambda *a: (["inc"], {"ok": True, "rows": 1}))
    monkeypatch.setattr(adapter, "_fetch_balance", lambda *a: (["bal"], {"ok": True, "rows": 1}))

    bundle = adapter.fetch_company_period("2330", "台積電", "TWSE", 2024, 1)

    assert bundle.incomes == ["inc"]
    assert bundle.balances == ["bal"]
    assert bundle.status == {"income": {"ok": True, "rows": 1}, "balance": {"ok": True, "rows": 1}}
