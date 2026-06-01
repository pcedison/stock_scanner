"""Branch coverage for the mock (sample-data) provider's query methods."""

from backend.models.settings import ScannerSettings
from backend.services.data_provider import MockDataProvider


def test_mock_provider_search_match_tiers():
    provider = MockDataProvider()
    assert provider.search_companies("   ") == []  # blank query short-circuits
    prefix = {c.stockCode for c in provider.search_companies("23")}
    assert {"2330", "2357"} <= prefix  # code-prefix tier
    industry = {c.stockCode for c in provider.search_companies("光電")}
    assert "3008" in industry  # industry-substring tier


def test_mock_provider_get_company_hit_and_miss():
    provider = MockDataProvider()
    assert provider.get_company("2330").stockCode == "2330"
    assert provider.get_company("0000") is None


def test_mock_provider_list_snapshots_respects_market_toggles():
    provider = MockDataProvider()
    twse_only = provider.list_snapshots(ScannerSettings(use_mock_data=True, scan_tpex=False))
    tpex_only = provider.list_snapshots(ScannerSettings(use_mock_data=True, scan_twse=False))
    assert {s.company.market for s in twse_only} == {"TWSE"}  # TPEX rows skipped
    assert {s.company.market for s in tpex_only} == {"TPEX"}  # TWSE rows skipped
