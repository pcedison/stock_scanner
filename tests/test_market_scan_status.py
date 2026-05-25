from backend.models.settings import ScannerSettings
from backend.services.market_scan import data_sources_status_payload


class FakeStatusProvider:
    def status(self, refresh=False):
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
                    "error": "OSError: private filesystem path",
                },
            },
        }


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
