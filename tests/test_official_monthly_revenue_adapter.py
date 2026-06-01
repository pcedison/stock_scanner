import httpx
import pytest

from backend.adapters.official_monthly_revenue import (
    OfficialMonthlyRevenueAdapter,
    roc_month_to_ad,
)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def test_fetch_json_retries_transient_failures(monkeypatch):
    calls = {"count": 0}

    def fake_get(url, timeout):
        calls["count"] += 1
        if calls["count"] == 1:
            raise httpx.ConnectError("temporary failure")
        return FakeResponse([{"stockCode": "1234"}])

    monkeypatch.setattr("backend.adapters.official_monthly_revenue.httpx.get", fake_get)
    adapter = OfficialMonthlyRevenueAdapter(retry_attempts=2, retry_backoff_seconds=0)

    assert adapter._fetch_json("https://example.test/data") == [{"stockCode": "1234"}]
    assert calls["count"] == 2


def test_fetch_json_rejects_non_list_payload(monkeypatch):
    def fake_get(url, timeout):
        return FakeResponse({"error": "temporary upstream response"})

    monkeypatch.setattr("backend.adapters.official_monthly_revenue.httpx.get", fake_get)
    adapter = OfficialMonthlyRevenueAdapter(retry_attempts=1, retry_backoff_seconds=0)

    with pytest.raises(RuntimeError, match="Failed to fetch official endpoint"):
        adapter._fetch_json("https://example.test/data")


def test_fetch_json_sleeps_between_retries_when_backoff_configured(monkeypatch):
    calls = {"count": 0}

    def fake_get(url, timeout):
        calls["count"] += 1
        if calls["count"] == 1:
            raise httpx.ConnectError("temporary failure")
        return FakeResponse([{"公司代號": "1234"}])

    slept = []
    monkeypatch.setattr("backend.adapters.official_monthly_revenue.httpx.get", fake_get)
    monkeypatch.setattr("backend.adapters.official_monthly_revenue.sleep", lambda s: slept.append(s))
    adapter = OfficialMonthlyRevenueAdapter(retry_attempts=2, retry_backoff_seconds=0.5)

    assert adapter._fetch_json("https://example.test/data") == [{"公司代號": "1234"}]
    assert slept == [0.5]  # backoff * attempt(1)


def test_roc_month_to_ad_returns_empty_for_unparseable():
    assert roc_month_to_ad("abcde") == ""
    assert roc_month_to_ad("123") == ""  # too short (<5 chars)


def _revenue_row(code="1234"):
    return {
        "公司代號": code,
        "公司名稱": "測試",
        "產業別": "半導體業",
        "出表日期": "1150512",
        "資料年月": "11504",
        "營業收入-當月營收": "1000",
        "營業收入-去年同月增減(%)": "10.5",
        "累計營業收入-當月累計營收": "4000",
        "累計營業收入-前期比較增減(%)": "8.0",
    }


def test_fetch_twse_and_tpex_monthly_revenue_parse_and_tag_market(monkeypatch):
    adapter = OfficialMonthlyRevenueAdapter(retry_attempts=1, retry_backoff_seconds=0)
    monkeypatch.setattr(adapter, "_fetch_json", lambda url: [_revenue_row(), {"公司代號": "x非數字"}])

    twse = adapter.fetch_twse_monthly_revenue()
    tpex = adapter.fetch_tpex_monthly_revenue()

    assert [r.stockCode for r in twse] == ["1234"]  # non-digit code skipped
    assert twse[0].market == "TWSE"
    assert twse[0].dataMonth == "2026-04"
    assert tpex[0].market == "TPEX"


def test_health_reports_per_endpoint_status(monkeypatch):
    adapter = OfficialMonthlyRevenueAdapter(retry_attempts=1, retry_backoff_seconds=0)
    monkeypatch.setattr(adapter, "_fetch_json", lambda url: [{"公司代號": "1234"}])

    status = adapter.health()

    assert set(status) == {
        "TWSE_COMPANY_PROFILE",
        "TPEX_COMPANY_PROFILE",
        "TWSE_MONTHLY_REVENUE",
        "TPEX_MONTHLY_REVENUE",
    }
    assert all(entry["ok"] and entry["rows"] == 1 for entry in status.values())
