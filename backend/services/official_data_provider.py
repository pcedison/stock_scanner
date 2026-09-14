from __future__ import annotations

from collections import Counter
from math import ceil
from threading import RLock
from time import monotonic
from typing import Literal, cast

from backend.adapters.fundamentals_history import OfficialFundamentalsHistoryStore
from backend.adapters.fundamentals_import import LocalFundamentalsImportAdapter
from backend.adapters.monthly_revenue_history import MonthlyRevenueHistoryStore
from backend.adapters.official_fundamentals import OfficialFundamentalsAdapter
from backend.adapters.official_monthly_revenue import (
    OfficialCompanyProfileRow,
    OfficialMonthlyRevenueAdapter,
    OfficialMonthlyRevenueRow,
)
from backend.models.company import Company
from backend.models.financial import FundamentalSnapshot
from backend.models.settings import ScannerSettings
from backend.services.data_provider import normalize_query

_EMPTY_COMPANY_PROFILES_ERROR = "official company profile refresh returned no rows"
_INCOMPLETE_COMPANY_PROFILES_ERROR = "official company profile refresh incomplete"
_CORE_PROFILE_MARKETS = ("TWSE", "TPEX")
_MIN_LAST_GOOD_MARKET_RATIO = 0.8

_FINANCIAL_KEYWORDS = frozenset({"金融", "銀行", "保險", "金控", "證券", "票券", "期貨", "投信", "投顧"})


def _is_financial_company(stock_code: str, industry_name: str, company_name: str) -> bool:
    text = f"{industry_name} {company_name}"
    if any(keyword in text for keyword in _FINANCIAL_KEYWORDS):
        return True
    try:
        return 2801 <= int(stock_code) <= 2999
    except ValueError:
        return False


def _latest_completed_quarter_from_month(month: str) -> str:
    try:
        year = int(month[:4])
        month_number = int(month[-2:])
    except ValueError:
        return "0000Q0"
    quarter = ((month_number - 1) // 3) + 1
    latest_completed = quarter - 1
    if latest_completed == 0:
        return f"{year - 1}Q4"
    return f"{year}Q{latest_completed}"


def _first_present(*values):
    for value in values:
        if value is not None:
            return value
    return None


class OfficialDataProvider:
    """Official TWSE/TPEx provider with separate light and heavy caches.

    Company search only needs the official universe. Full market scans need
    monthly revenue, statements, valuations, imports, and local history. Keeping
    those refresh paths separate prevents typing in the search box or loading a
    status panel from triggering a full financial-data refresh.
    """

    def __init__(
        self,
        adapter: OfficialMonthlyRevenueAdapter | None = None,
        fundamentals_adapter: OfficialFundamentalsAdapter | None = None,
        import_adapter: LocalFundamentalsImportAdapter | None = None,
        history_store: OfficialFundamentalsHistoryStore | None = None,
        monthly_revenue_history: MonthlyRevenueHistoryStore | None = None,
        ttl_seconds: int = 900,
    ) -> None:
        self.adapter = adapter or OfficialMonthlyRevenueAdapter()
        self.fundamentals_adapter = fundamentals_adapter or OfficialFundamentalsAdapter()
        self.import_adapter = import_adapter or LocalFundamentalsImportAdapter()
        self.history_store = history_store or OfficialFundamentalsHistoryStore()
        self.monthly_revenue_history = monthly_revenue_history or MonthlyRevenueHistoryStore()
        self.ttl_seconds = ttl_seconds
        self._profiles_expires_at = 0.0
        self._snapshots_expires_at = 0.0
        self._companies: list[Company] = []
        self._snapshots: dict[str, FundamentalSnapshot] = {}
        self._last_error: str | None = None
        self._source_status: dict = {}
        self._refresh_lock = RLock()

    def _needs_profiles_refresh(self) -> bool:
        return monotonic() >= self._profiles_expires_at or not self._companies

    def _needs_snapshots_refresh(self) -> bool:
        return monotonic() >= self._snapshots_expires_at or not self._snapshots

    def _record_empty_company_profiles(self) -> None:
        self._profiles_expires_at = monotonic() + self.ttl_seconds
        self._last_error = _EMPTY_COMPANY_PROFILES_ERROR
        self._source_status = {
            **self._source_status,
            "companyProfiles": 0,
            "companyProfilesNormalized": 0,
            "companyProfilesByMarket": dict.fromkeys(_CORE_PROFILE_MARKETS, 0),
            "companyProfilesAccepted": False,
        }

    @staticmethod
    def _company_market_counts(companies: list[Company]) -> dict[str, int]:
        counts: Counter[str] = Counter(company.market for company in companies)
        return {market: counts.get(market, 0) for market in _CORE_PROFILE_MARKETS}

    def _profile_refresh_problem(self, companies: list[Company]) -> str | None:
        if not companies:
            return "no valid rows after normalization"
        previous = self._company_market_counts(self._companies)
        current = self._company_market_counts(companies)
        missing_markets = [market for market in _CORE_PROFILE_MARKETS if previous[market] > 0 and current[market] == 0]
        if missing_markets:
            return f"missing previously cached core markets: {', '.join(missing_markets)}"
        for market in _CORE_PROFILE_MARKETS:
            previous_count = previous[market]
            if previous_count <= 0:
                continue
            minimum = max(1, ceil(previous_count * _MIN_LAST_GOOD_MARKET_RATIO))
            if current[market] < minimum:
                return f"{market} coverage {current[market]}/{previous_count} below {minimum}"
        return None

    def _record_incomplete_company_profiles(
        self,
        raw_count: int,
        companies: list[Company],
        problem: str,
    ) -> None:
        self._profiles_expires_at = monotonic() + self.ttl_seconds
        self._last_error = f"{_INCOMPLETE_COMPANY_PROFILES_ERROR}: {problem}"
        self._source_status = {
            **self._source_status,
            "companyProfiles": raw_count,
            "companyProfilesNormalized": len(companies),
            "companyProfilesByMarket": self._company_market_counts(companies),
            "companyProfilesAccepted": False,
        }

    def _company_from_profile(
        self,
        profile: OfficialCompanyProfileRow,
        revenue: OfficialMonthlyRevenueRow | None = None,
    ) -> Company | None:
        if not profile.stockCode or not profile.stockCode.isdigit():
            return None
        company_name = (
            revenue.companyName if revenue and revenue.companyName else profile.companyShortName or profile.companyName
        ).strip()
        industry_name = (
            revenue.industryName if revenue and revenue.industryName else profile.industryName or profile.industryCode
        ).strip()
        company_name = company_name or profile.stockCode
        industry_name = industry_name or "Unknown industry"
        return Company(
            stockCode=profile.stockCode,
            name=company_name,
            # profile.market is a plain str; Company still validates it against the
            # Literal at runtime, so this cast is type-only and behaviour-preserving.
            market=cast(Literal["TWSE", "TPEX", "OTHER"], profile.market),
            industryName=industry_name,
            isFinancial=_is_financial_company(profile.stockCode, industry_name, company_name),
        )

    def refresh_companies(self, force: bool = False) -> None:
        if not force and not self._needs_profiles_refresh():
            return
        with self._refresh_lock:
            if not force and not self._needs_profiles_refresh():
                return
            profiles = self.adapter.fetch_company_profiles()
            if not profiles:
                self._record_empty_company_profiles()
                return
            companies = [company for profile in profiles if (company := self._company_from_profile(profile))]
            if problem := self._profile_refresh_problem(companies):
                self._record_incomplete_company_profiles(len(profiles), companies, problem)
                return
            self._companies = sorted(companies, key=lambda company: company.stockCode)
            self._profiles_expires_at = monotonic() + self.ttl_seconds
            self._last_error = None
            self._source_status = {
                **self._source_status,
                "companyProfiles": len(companies),
                "companyProfilesNormalized": len(companies),
                "companyProfilesByMarket": self._company_market_counts(companies),
                "companyProfilesAccepted": True,
                "companyProfilesFallback": dict(getattr(self.adapter, "profile_fallbacks", {}) or {}),
            }

    def refresh(self, force: bool = False) -> None:
        if not force and not self._needs_snapshots_refresh():
            return
        with self._refresh_lock:
            if not force and not self._needs_snapshots_refresh():
                return

            profiles = self.adapter.fetch_company_profiles()
            if not profiles:
                self._record_empty_company_profiles()
                if self._snapshots:
                    self._snapshots_expires_at = self._profiles_expires_at
                return
            profile_companies = [company for profile in profiles if (company := self._company_from_profile(profile))]
            if problem := self._profile_refresh_problem(profile_companies):
                self._record_incomplete_company_profiles(len(profiles), profile_companies, problem)
                if self._snapshots:
                    self._snapshots_expires_at = self._profiles_expires_at
                return
            # Profiles fetched as part of full refresh — update profile TTL immediately
            # so a concurrent list_companies() call doesn't trigger a redundant profile fetch.
            self._profiles_expires_at = monotonic() + self.ttl_seconds
            revenue_rows = self.adapter.fetch_monthly_revenue()
            fundamentals = self.fundamentals_adapter.fetch_bundle()
            imported = self.import_adapter.fetch_bundle()
            try:
                history_status = self.history_store.merge_latest(fundamentals.incomes, fundamentals.balances)
            except Exception:  # pragma: no cover - depends on local filesystem state
                history_status = {"enabled": False, "path": str(self.history_store.path), "hasError": True}
            try:
                monthly_history_status = self.monthly_revenue_history.merge_rows(revenue_rows)
            except Exception:  # pragma: no cover - depends on local filesystem state
                monthly_history_status = {
                    "enabled": False,
                    "path": str(self.monthly_revenue_history.path),
                    "hasError": True,
                }
            revenue_by_code = {row.stockCode: row for row in revenue_rows if row.stockCode}
            companies: list[Company] = []
            snapshots: dict[str, FundamentalSnapshot] = {}

            for profile in profiles:
                revenue = revenue_by_code.get(profile.stockCode)
                company = self._company_from_profile(profile, revenue)
                if company is None:
                    continue
                companies.append(company)

                imported_monthly = imported.monthly.get(company.stockCode)
                snapshot_month = _first_present(
                    imported_monthly.month if imported_monthly else None,
                    revenue.dataMonth if revenue else None,
                )
                if not snapshot_month:
                    continue

                income = fundamentals.incomes.get(company.stockCode)
                balance = fundamentals.balances.get(company.stockCode)
                valuation = fundamentals.valuations.get(company.stockCode)
                imported_quarterly = imported.quarterly.get(company.stockCode)
                imported_valuation = imported.valuations.get(company.stockCode)
                latest_quarter = _latest_completed_quarter_from_month(snapshot_month)
                if income and income.fiscalYear and income.quarter:
                    latest_quarter = f"{income.fiscalYear}Q{income.quarter}"
                if imported_quarterly and imported_quarterly.period:
                    latest_quarter = imported_quarterly.period

                history_quarter = self.history_store.quarter(company.stockCode, latest_quarter)
                if (
                    history_quarter is None
                    and not (income and income.fiscalYear and income.quarter)
                    and not (imported_quarterly and imported_quarterly.period)
                ):
                    history_quarter = self.history_store.latest_quarter(company.stockCode)
                    if history_quarter:
                        latest_quarter = history_quarter.period
                history_yoy = self.history_store.quarterly_yoy(company.stockCode, latest_quarter)

                current_inventory_turnover = None
                if (
                    income
                    and balance
                    and income.costOfRevenue is not None
                    and balance.inventory is not None
                    and balance.inventory != 0
                ):
                    annualized_factor = 4 / income.quarter if income.quarter else 1
                    current_inventory_turnover = (income.costOfRevenue * annualized_factor) / balance.inventory
                inventory_turnover = _first_present(
                    imported_valuation.inventoryTurnover if imported_valuation else None,
                    self.history_store.inventory_turnover(company.stockCode, latest_quarter),
                    current_inventory_turnover,
                )

                annuals = []
                annuals.extend(self.history_store.annual_financials(company.stockCode))
                if income and income.quarter == 4 and income.fiscalYear:
                    annuals.append({"year": income.fiscalYear, "netIncome": income.netIncome})
                annuals.extend(
                    {"year": row.year, "netIncome": row.netIncome}
                    for row in imported.annuals.get(company.stockCode, [])
                )
                annuals = list(
                    {row["year"]: row for row in sorted(annuals, key=lambda item: cast(int, item["year"]))}.values()
                )

                try:
                    snapshot_year = int(snapshot_month[:4])
                except (ValueError, TypeError):
                    snapshot_year = 0
                snapshots[company.stockCode] = FundamentalSnapshot.model_validate(
                    {
                        "company": company.model_dump(),
                        "monthlyRevenue": {
                            "month": snapshot_month,
                            "monthlyRevenueYoY": _first_present(
                                imported_monthly.monthlyRevenueYoY if imported_monthly else None,
                                revenue.monthlyRevenueYoY if revenue else None,
                            ),
                            "previousMonthRevenueYoY": _first_present(
                                imported_monthly.previousMonthRevenueYoY if imported_monthly else None,
                                self.monthly_revenue_history.previous_month_yoy(company.stockCode, snapshot_month),
                            ),
                            "cumulativeRevenueYoY": _first_present(
                                imported_monthly.cumulativeRevenueYoY if imported_monthly else None,
                                revenue.cumulativeRevenueYoY if revenue else None,
                            ),
                            "trailingThreeMonthAverageYoY": _first_present(
                                imported_monthly.trailingThreeMonthAverageYoY if imported_monthly else None,
                                self.monthly_revenue_history.trailing_three_month_avg_yoy(
                                    company.stockCode, snapshot_month
                                ),
                            ),
                            "janFebCombinedRevenueYoY": _first_present(
                                imported_monthly.janFebCombinedRevenueYoY if imported_monthly else None,
                                self.monthly_revenue_history.jan_feb_combined_yoy(company.stockCode, snapshot_year)
                                if snapshot_year
                                else None,
                            ),
                            "isSpringFestivalMonth": imported_monthly.isSpringFestivalMonth
                            if imported_monthly and imported_monthly.isSpringFestivalMonth is not None
                            else False,
                        },
                        "quarterlyFinancial": {
                            "quarter": latest_quarter,
                            "eps": _first_present(
                                imported_quarterly.eps if imported_quarterly else None,
                                income.eps if income else None,
                                history_quarter.eps if history_quarter else None,
                            ),
                            "epsYoY": _first_present(
                                imported_quarterly.epsYoY if imported_quarterly else None,
                                history_yoy.get("epsYoY"),
                            ),
                            "netIncome": _first_present(
                                imported_quarterly.netIncome if imported_quarterly else None,
                                income.netIncome if income else None,
                                history_quarter.netIncome if history_quarter else None,
                            ),
                            "netIncomeYoY": _first_present(
                                imported_quarterly.netIncomeYoY if imported_quarterly else None,
                                history_yoy.get("netIncomeYoY"),
                            ),
                            "revenue": _first_present(
                                imported_quarterly.revenue if imported_quarterly else None,
                                income.revenue if income else None,
                                history_quarter.revenue if history_quarter else None,
                            ),
                            "grossMargin": _first_present(
                                imported_quarterly.grossMargin if imported_quarterly else None,
                                income.grossMargin if income else None,
                                history_quarter.grossMargin if history_quarter else None,
                            ),
                            "grossMarginYoY": _first_present(
                                imported_quarterly.grossMarginYoY if imported_quarterly else None,
                                history_yoy.get("grossMarginYoY"),
                            ),
                            "operatingMargin": _first_present(
                                imported_quarterly.operatingMargin if imported_quarterly else None,
                                income.operatingMargin if income else None,
                            ),
                        },
                        "valuation": {
                            "per": _first_present(
                                imported_valuation.per if imported_valuation else None,
                                valuation.per if valuation else None,
                            ),
                            "priceBookRatio": _first_present(
                                imported_valuation.priceBookRatio if imported_valuation else None,
                                valuation.priceBookRatio if valuation else None,
                            ),
                            "dividendYield": _first_present(
                                imported_valuation.dividendYield if imported_valuation else None,
                                valuation.dividendYield if valuation else None,
                            ),
                            "valuationDate": _first_present(
                                imported_valuation.valuationDate if imported_valuation else None,
                                valuation.date if valuation else None,
                            ),
                            "valuationFiscalQuarter": _first_present(
                                imported_valuation.valuationFiscalQuarter if imported_valuation else None,
                                valuation.fiscalQuarter if valuation else None,
                            ),
                            "inventoryTurnover": inventory_turnover,
                            "roe": imported_valuation.roe if imported_valuation else None,
                            "nonPerformingLoanRatio": imported_valuation.nonPerformingLoanRatio
                            if imported_valuation
                            else None,
                            "capitalAdequacyRatio": imported_valuation.capitalAdequacyRatio
                            if imported_valuation
                            else None,
                            "netInterestMargin": imported_valuation.netInterestMargin if imported_valuation else None,
                        },
                        "annualFinancials": annuals,
                    }
                )

            now = monotonic()
            self._companies = sorted(companies, key=lambda company: company.stockCode)
            self._snapshots = snapshots
            self._profiles_expires_at = now + self.ttl_seconds
            self._snapshots_expires_at = now + self.ttl_seconds
            self._last_error = None
            self._source_status = {
                "companyProfiles": len(companies),
                "companyProfilesNormalized": len(companies),
                "companyProfilesByMarket": self._company_market_counts(companies),
                "companyProfilesAccepted": True,
                "companyProfilesFallback": dict(getattr(self.adapter, "profile_fallbacks", {}) or {}),
                "monthlyRevenueRows": len(revenue_rows),
                **fundamentals.status,
                "fundamentalsImport": imported.status,
                "officialFundamentalsHistory": history_status,
                "monthlyRevenueHistory": monthly_history_status,
            }

    def _safe_refresh_companies(self) -> None:
        try:
            self.refresh_companies()
        except Exception as exc:  # pragma: no cover - depends on official network availability
            self._last_error = f"official company profile refresh failed: {exc}"
            if not self._companies:
                self._companies = []
                self._source_status = {**self._source_status, "companyProfiles": 0}

    def _safe_refresh_snapshots(self) -> None:
        try:
            self.refresh()
        except Exception as exc:  # pragma: no cover - depends on official network availability
            self._last_error = f"official snapshot refresh failed: {exc}"
            if not self._snapshots:
                self._snapshots = {}

    def list_companies(self) -> list[Company]:
        self._safe_refresh_companies()
        return list(self._companies)

    def search_companies(self, query: str, limit: int = 20) -> list[Company]:
        self._safe_refresh_companies()
        normalized = normalize_query(query)
        if not normalized:
            return []

        scored: list[tuple[int, Company]] = []
        for company in self._companies:
            code = company.stockCode.lower()
            name = company.name.lower()
            haystack = f"{code} {name} {company.industryName.lower()}"
            if normalized in (code, name):
                scored.append((0, company))
            elif code.startswith(normalized) or name.startswith(normalized):
                scored.append((1, company))
            elif normalized in haystack:
                scored.append((2, company))

        return [company for _, company in sorted(scored, key=lambda item: (item[0], item[1].stockCode))[:limit]]

    def get_company(self, stock_code: str) -> Company | None:
        self._safe_refresh_companies()
        return next((company for company in self._companies if company.stockCode == stock_code), None)

    def get_snapshot(self, stock_code: str) -> FundamentalSnapshot | None:
        self._safe_refresh_snapshots()
        return self._snapshots.get(stock_code)

    def list_snapshots(self, settings: ScannerSettings) -> list[FundamentalSnapshot]:
        self._safe_refresh_snapshots()
        snapshots = []
        for snapshot in self._snapshots.values():
            if snapshot.company.market == "TWSE" and not settings.scan_twse:
                continue
            if snapshot.company.market == "TPEX" and not settings.scan_tpex:
                continue
            snapshots.append(snapshot)
        return sorted(snapshots, key=lambda snapshot: snapshot.company.stockCode)

    def status(self, refresh: bool = False, expected_period: str | None = None) -> dict:
        if refresh:
            self._safe_refresh_snapshots()
        elif not self._companies:
            self._safe_refresh_companies()
        previous_history_status = self._source_status.get("officialFundamentalsHistory")
        history_status = self.history_store.status(expected_period=expected_period)
        if isinstance(previous_history_status, dict):
            history_status = {**previous_history_status, **history_status}
        source_status = {**self._source_status, "officialFundamentalsHistory": history_status}
        return {
            "companies": len(self._companies),
            "monthlySnapshots": len(self._snapshots),
            "cacheTtlSeconds": self.ttl_seconds,
            "lastError": self._last_error,
            "sourceStatus": source_status,
        }
