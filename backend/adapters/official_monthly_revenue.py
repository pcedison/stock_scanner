from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from time import sleep
from typing import Any

import httpx

from backend.adapters._utils import to_float as _to_float
from backend.adapters.official_tls import official_ssl_context

TWSE_COMPANY_PROFILE_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
TPEX_COMPANY_PROFILE_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"
TWSE_MONTHLY_REVENUE_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap05_L"
TPEX_MONTHLY_REVENUE_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O"


@dataclass(frozen=True)
class OfficialCompanyProfileRow:
    stockCode: str
    companyName: str
    companyShortName: str
    market: str
    industryName: str
    industryCode: str
    reportDate: str


@dataclass(frozen=True)
class OfficialMonthlyRevenueRow:
    stockCode: str
    companyName: str
    market: str
    industryName: str
    reportDate: str
    dataMonth: str
    monthlyRevenue: int | None
    monthlyRevenueYoY: float | None
    cumulativeRevenue: int | None
    cumulativeRevenueYoY: float | None


def roc_month_to_ad(value: Any) -> str:
    text = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(text) < 5:
        return ""
    try:
        year = int(text[:-2]) + 1911
        month = int(text[-2:])
    except ValueError:  # pragma: no cover - text is all-digit and len>=5, int() cannot raise
        return ""
    if not 1 <= month <= 12:
        return ""
    return f"{year:04d}-{month:02d}"


def _to_int(value: Any) -> int | None:
    text = str(value or "").replace(",", "").strip()
    if not text or text in {"-", "--"}:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None




class OfficialMonthlyRevenueAdapter:
    def __init__(self, timeout: float = 15, retry_attempts: int = 3, retry_backoff_seconds: float = 1) -> None:
        self.timeout = timeout
        self.retry_attempts = max(1, retry_attempts)
        self.retry_backoff_seconds = max(0.0, retry_backoff_seconds)
        # Markets whose last fetch_company_profiles() fell back, e.g. {"TPEX": "monthly_revenue"}.
        self.profile_fallbacks: dict[str, str] = {}

    def _fetch_json(self, url: str) -> list[dict[str, Any]]:
        last_error: Exception | None = None
        for attempt in range(1, self.retry_attempts + 1):
            try:
                response = httpx.get(url, timeout=self.timeout, verify=official_ssl_context())
                response.raise_for_status()
                rows = response.json()
                if not isinstance(rows, list):
                    raise ValueError(f"official endpoint returned {type(rows).__name__}, expected list")
                return rows
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                if attempt < self.retry_attempts and self.retry_backoff_seconds:
                    sleep(self.retry_backoff_seconds * attempt)
        raise RuntimeError(f"Failed to fetch official endpoint after {self.retry_attempts} attempts: {url}") from last_error

    def _fetch_monthly_revenue(self, url: str, market: str) -> list[OfficialMonthlyRevenueRow]:
        rows = self._fetch_json(url)
        result = []
        for row in rows:
            stock_code = str(row.get("公司代號", "")).strip()
            if not stock_code or not stock_code.isdigit():
                continue
            result.append(OfficialMonthlyRevenueRow(
                stockCode=stock_code,
                companyName=str(row.get("公司名稱", "")).strip(),
                market=market,
                industryName=str(row.get("產業別", "")).strip(),
                reportDate=str(row.get("出表日期", "")).strip(),
                dataMonth=roc_month_to_ad(row.get("資料年月")) or str(row.get("資料年月", "")).strip(),
                monthlyRevenue=_to_int(row.get("營業收入-當月營收")),
                monthlyRevenueYoY=_to_float(row.get("營業收入-去年同月增減(%)")),
                cumulativeRevenue=_to_int(row.get("累計營業收入-當月累計營收")),
                cumulativeRevenueYoY=_to_float(row.get("累計營業收入-前期比較增減(%)")),
            ))
        return result

    def fetch_twse_monthly_revenue(self) -> list[OfficialMonthlyRevenueRow]:
        return self._fetch_monthly_revenue(TWSE_MONTHLY_REVENUE_URL, "TWSE")

    def fetch_tpex_monthly_revenue(self) -> list[OfficialMonthlyRevenueRow]:
        return self._fetch_monthly_revenue(TPEX_MONTHLY_REVENUE_URL, "TPEX")

    def fetch_monthly_revenue(self) -> list[OfficialMonthlyRevenueRow]:
        with ThreadPoolExecutor(max_workers=2) as executor:
            twse = executor.submit(self.fetch_twse_monthly_revenue)
            tpex = executor.submit(self.fetch_tpex_monthly_revenue)
            return [*twse.result(), *tpex.result()]

    def fetch_twse_company_profiles(self) -> list[OfficialCompanyProfileRow]:
        rows = self._fetch_json(TWSE_COMPANY_PROFILE_URL)
        return [
            OfficialCompanyProfileRow(
                stockCode=str(row.get("公司代號", "")).strip(),
                companyName=str(row.get("公司名稱", "")).strip(),
                companyShortName=str(row.get("公司簡稱", "") or row.get("公司名稱", "")).strip(),
                market="TWSE",
                industryName=str(row.get("產業別", "")).strip(),
                industryCode=str(row.get("產業別", "")).strip(),
                reportDate=str(row.get("出表日期", "")).strip(),
            )
            for row in rows
        ]

    def fetch_tpex_company_profiles(self) -> list[OfficialCompanyProfileRow]:
        rows = self._fetch_json(TPEX_COMPANY_PROFILE_URL)
        return [
            OfficialCompanyProfileRow(
                stockCode=str(row.get("SecuritiesCompanyCode", "")).strip(),
                companyName=str(row.get("CompanyName", "")).strip(),
                companyShortName=str(row.get("CompanyAbbreviation", "") or row.get("CompanyName", "")).strip(),
                market="TPEX",
                industryName=str(row.get("SecuritiesIndustryCode", "")).strip(),
                industryCode=str(row.get("SecuritiesIndustryCode", "")).strip(),
                reportDate=str(row.get("Date", "")).strip(),
            )
            for row in rows
        ]

    def _profiles_from_monthly_revenue(self, market: str) -> list[OfficialCompanyProfileRow]:
        fetch = self.fetch_twse_monthly_revenue if market == "TWSE" else self.fetch_tpex_monthly_revenue
        return [
            OfficialCompanyProfileRow(
                stockCode=row.stockCode,
                companyName=row.companyName,
                companyShortName=row.companyName,
                market=market,
                industryName=row.industryName,
                industryCode=row.industryName,
                reportDate=row.reportDate,
            )
            for row in fetch()
            if row.stockCode.isdigit()
        ]

    def fetch_company_profiles(self) -> list[OfficialCompanyProfileRow]:
        """Both markets' profiles; a market whose profile endpoint is down is derived from its
        monthly-revenue file (same code/name/industry columns) so one broken endpoint cannot
        zero the whole scan universe. Raises only when the fallback fails too."""
        self.profile_fallbacks = {}
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = {
                "TWSE": executor.submit(self.fetch_twse_company_profiles),
                "TPEX": executor.submit(self.fetch_tpex_company_profiles),
            }
            profiles: list[OfficialCompanyProfileRow] = []
            for market, future in futures.items():
                try:
                    profiles.extend(future.result())
                except RuntimeError as exc:
                    try:
                        derived = self._profiles_from_monthly_revenue(market)
                    except RuntimeError:
                        raise exc from None
                    if not derived:
                        raise
                    self.profile_fallbacks[market] = "monthly_revenue"
                    profiles.extend(derived)
            return profiles

    def health(self) -> dict:
        status = {}
        for market, url in {
            "TWSE_COMPANY_PROFILE": TWSE_COMPANY_PROFILE_URL,
            "TPEX_COMPANY_PROFILE": TPEX_COMPANY_PROFILE_URL,
            "TWSE_MONTHLY_REVENUE": TWSE_MONTHLY_REVENUE_URL,
            "TPEX_MONTHLY_REVENUE": TPEX_MONTHLY_REVENUE_URL,
        }.items():
            try:
                rows = self._fetch_json(url)
                status[market] = {"ok": True, "url": url, "rows": len(rows)}
            except Exception as exc:  # pragma: no cover - network availability is environment-dependent
                status[market] = {"ok": False, "url": url, "error": str(exc)}
        return status
