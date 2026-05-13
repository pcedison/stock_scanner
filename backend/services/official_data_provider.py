from __future__ import annotations

from time import monotonic
from typing import Optional

from backend.adapters.official_monthly_revenue import OfficialMonthlyRevenueAdapter
from backend.models.company import Company
from backend.models.financial import FundamentalSnapshot
from backend.models.settings import ScannerSettings
from backend.services.data_provider import normalize_query


def _is_financial_company(stock_code: str, industry_name: str, company_name: str) -> bool:
    text = f"{stock_code} {industry_name} {company_name}"
    return stock_code.startswith("28") or "金融" in text or "銀行" in text or "保險" in text


def _quarter_from_month(month: str) -> str:
    try:
        year = int(month[:4])
        month_number = int(month[-2:])
    except ValueError:
        return "0000Q0"
    quarter = ((month_number - 1) // 3) + 1
    return f"{year}Q{quarter}"


class OfficialDataProvider:
    """Official TWSE/TPEx provider for public universe and latest monthly revenue.

    This provider intentionally returns incomplete fundamental snapshots until the
    quarterly, annual, valuation, and inventory adapters are implemented. The rule
    engine then emits INSUFFICIENT_DATA instead of pretending those metrics exist.
    """

    def __init__(self, adapter: Optional[OfficialMonthlyRevenueAdapter] = None, ttl_seconds: int = 900) -> None:
        self.adapter = adapter or OfficialMonthlyRevenueAdapter()
        self.ttl_seconds = ttl_seconds
        self._expires_at = 0.0
        self._companies: list[Company] = []
        self._snapshots: dict[str, FundamentalSnapshot] = {}
        self._last_error: str | None = None

    def _needs_refresh(self) -> bool:
        return monotonic() >= self._expires_at or not self._companies

    def refresh(self, force: bool = False) -> None:
        if not force and not self._needs_refresh():
            return

        profiles = self.adapter.fetch_company_profiles()
        revenue_rows = self.adapter.fetch_monthly_revenue()
        revenue_by_code = {row.stockCode: row for row in revenue_rows if row.stockCode}
        companies: list[Company] = []
        snapshots: dict[str, FundamentalSnapshot] = {}

        for profile in profiles:
            if not profile.stockCode or not profile.stockCode.isdigit():
                continue
            revenue = revenue_by_code.get(profile.stockCode)
            company_name = (revenue.companyName if revenue and revenue.companyName else profile.companyShortName or profile.companyName).strip()
            industry_name = (revenue.industryName if revenue and revenue.industryName else profile.industryName or profile.industryCode).strip()
            if not company_name:
                company_name = profile.stockCode
            if not industry_name:
                industry_name = "未分類"

            company = Company(
                stockCode=profile.stockCode,
                name=company_name,
                market=profile.market,
                industryName=industry_name,
                isFinancial=_is_financial_company(profile.stockCode, industry_name, company_name),
            )
            companies.append(company)

            if revenue and revenue.dataMonth:
                snapshots[company.stockCode] = FundamentalSnapshot.model_validate(
                    {
                        "company": company.model_dump(),
                        "monthlyRevenue": {
                            "month": revenue.dataMonth,
                            "monthlyRevenueYoY": revenue.monthlyRevenueYoY,
                            "previousMonthRevenueYoY": None,
                            "cumulativeRevenueYoY": revenue.cumulativeRevenueYoY,
                            "trailingThreeMonthAverageYoY": None,
                            "janFebCombinedRevenueYoY": None,
                            "isSpringFestivalMonth": False,
                        },
                        "quarterlyFinancial": {
                            "quarter": _quarter_from_month(revenue.dataMonth),
                            "epsYoY": None,
                            "netIncomeYoY": None,
                            "grossMarginYoY": None,
                        },
                        "valuation": {"per": None, "inventoryTurnover": None},
                        "annualFinancials": [],
                    }
                )

        self._companies = sorted(companies, key=lambda company: company.stockCode)
        self._snapshots = snapshots
        self._expires_at = monotonic() + self.ttl_seconds
        self._last_error = None

    def _safe_refresh(self) -> None:
        try:
            self.refresh()
        except Exception as exc:  # pragma: no cover - depends on official network availability
            self._last_error = str(exc)
            if not self._companies:
                self._companies = []
                self._snapshots = {}

    def list_companies(self) -> list[Company]:
        self._safe_refresh()
        return list(self._companies)

    def search_companies(self, query: str, limit: int = 20) -> list[Company]:
        self._safe_refresh()
        normalized = normalize_query(query)
        if not normalized:
            return []

        scored: list[tuple[int, Company]] = []
        for company in self._companies:
            code = company.stockCode.lower()
            name = company.name.lower()
            haystack = f"{code} {name} {company.industryName.lower()}"
            if normalized == code or normalized == name:
                scored.append((0, company))
            elif code.startswith(normalized) or name.startswith(normalized):
                scored.append((1, company))
            elif normalized in haystack:
                scored.append((2, company))

        return [company for _, company in sorted(scored, key=lambda item: (item[0], item[1].stockCode))[:limit]]

    def get_company(self, stock_code: str) -> Optional[Company]:
        self._safe_refresh()
        return next((company for company in self._companies if company.stockCode == stock_code), None)

    def get_snapshot(self, stock_code: str) -> Optional[FundamentalSnapshot]:
        self._safe_refresh()
        return self._snapshots.get(stock_code)

    def iter_snapshots(self, settings: ScannerSettings) -> list[FundamentalSnapshot]:
        self._safe_refresh()
        snapshots = []
        for snapshot in self._snapshots.values():
            if snapshot.company.market == "TWSE" and not settings.scan_twse:
                continue
            if snapshot.company.market == "TPEX" and not settings.scan_tpex:
                continue
            snapshots.append(snapshot)
        return sorted(snapshots, key=lambda snapshot: snapshot.company.stockCode)

    def status(self) -> dict:
        self._safe_refresh()
        return {
            "companies": len(self._companies),
            "monthlySnapshots": len(self._snapshots),
            "cacheTtlSeconds": self.ttl_seconds,
            "lastError": self._last_error,
        }
