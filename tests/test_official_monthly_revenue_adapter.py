import httpx
import pytest

from backend.adapters.official_monthly_revenue import OfficialMonthlyRevenueAdapter


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
