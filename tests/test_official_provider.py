from backend.adapters.official_monthly_revenue import OfficialCompanyProfileRow, OfficialMonthlyRevenueRow, roc_month_to_ad
from backend.models.settings import ScannerSettings
from backend.services.official_data_provider import OfficialDataProvider
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


def test_roc_month_to_ad_converts_official_month():
    assert roc_month_to_ad("11504") == "2026-04"


def test_official_provider_builds_universe_and_marks_missing_fundamentals():
    provider = OfficialDataProvider(adapter=FakeOfficialAdapter())

    companies = provider.list_companies()
    snapshot = provider.get_snapshot("9999")
    result = RuleEngine().evaluate_entry(snapshot, ScannerSettings(use_mock_data=False))

    assert len(companies) == 2
    assert provider.status()["monthlySnapshots"] == 1
    assert snapshot.company.name == "測試科技"
    assert result.status == "INSUFFICIENT_DATA"
    assert {reason.code for reason in result.reasons} >= {"E1", "E2", "E4", "E5"}
