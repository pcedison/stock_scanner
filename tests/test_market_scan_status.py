from datetime import date

from backend.models.settings import ScannerSettings
from backend.services.filing_calendar import filing_context
from backend.services.market_scan import data_sources_status_payload, scan_market_payload


def _patch_filing_context(monkeypatch, today=None):
    target_date = today or date(2026, 6, 22)
    monkeypatch.setattr("backend.services.market_scan.filing_context", lambda: filing_context(target_date))


class FakeStatusProvider:
    def list_snapshots(self, settings):
        return []

    def status(self, refresh=False, expected_period=None):
        return {
            "companies": 10,
            "monthlySnapshots": 9,
            "lastError": "internal stack trace",
            "sourceStatus": {
                "incomeRows": 3,
                "fundamentalsImport": {"rows": 2, "path": "data/fundamentals.csv"},
                "officialFundamentalsHistory": {
                    "rows": 5,
                    "path": "data/history.json",
                    "latestFinancialPeriod": "2025Q4",
                    "periodCoverage": {"2025Q4": 5},
                    "expectedPeriodCoverage": 0,
                    "error": "OSError: private filesystem path",
                },
            },
        }


class FakeRefreshingStatusProvider(FakeStatusProvider):
    def __init__(self):
        self.refreshed = False

    def list_snapshots(self, settings):
        self.refreshed = True
        return []

    def status(self, refresh=False, expected_period=None):
        payload = super().status(refresh=refresh, expected_period=expected_period)
        history = payload["sourceStatus"]["officialFundamentalsHistory"]
        if self.refreshed:
            period = expected_period or "2026Q1"
            history["latestFinancialPeriod"] = period
            history["periodCoverage"] = {period: 5}
            history["expectedPeriodCoverage"] = 5
        return payload


def test_data_sources_status_redacts_provider_error_details():
    payload = data_sources_status_payload(
        ScannerSettings(use_mock_data=False),
        FakeStatusProvider(),
        mock_universe_size=0,
    )

    cache = payload["officialHistoricalFundamentals"]["cache"]
    assert cache["hasError"] is True
    assert "error" not in cache
    assert "private filesystem path" not in str(payload)


def test_data_sources_status_redacts_scan_cache_job_error_details():
    payload = data_sources_status_payload(
        ScannerSettings(use_mock_data=False),
        FakeStatusProvider(),
        mock_universe_size=0,
        scan_cache_status={
            "recentJobs": [
                {
                    "id": "job-1",
                    "status": "failed",
                    "error": "RuntimeError: private cache path",
                }
            ]
        },
    )

    job = payload["marketScanCache"]["recentJobs"][0]
    assert job["hasError"] is True
    assert "error" not in job
    assert "private cache path" not in str(payload)


def test_data_sources_status_reports_financial_freshness_gate(monkeypatch):
    _patch_filing_context(monkeypatch)

    payload = data_sources_status_payload(
        ScannerSettings(use_mock_data=False),
        FakeStatusProvider(),
        mock_universe_size=0,
    )

    freshness = payload["financialFreshness"]
    assert freshness["status"] == "stale"
    assert freshness["expectedFinancialPeriod"] == "2026Q1"
    assert freshness["latestCachedFinancialPeriod"] == "2025Q4"
    assert freshness["blocksDeployment"] is True


def test_scan_market_payload_reports_financial_freshness_without_changing_scan_groups():
    payload = scan_market_payload(
        ScannerSettings(use_mock_data=False),
        FakeStatusProvider(),
        engine=object(),
    )

    assert payload["entry"] == []
    assert payload["watch"] == []
    assert payload["excluded"] == []
    assert payload["financialFreshness"]["status"] == "stale"


def test_scan_market_payload_checks_financial_freshness_after_provider_refresh(monkeypatch):
    _patch_filing_context(monkeypatch, date(2026, 11, 15))

    payload = scan_market_payload(
        ScannerSettings(use_mock_data=False),
        FakeRefreshingStatusProvider(),
        engine=object(),
    )

    assert payload["financialFreshness"]["status"] == "ok"
    assert payload["financialFreshness"]["expectedFinancialPeriod"] == "2026Q3"
    assert payload["financialFreshness"]["latestCachedFinancialPeriod"] == "2026Q3"
