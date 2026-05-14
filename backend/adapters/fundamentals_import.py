from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend.adapters.official_monthly_revenue import roc_month_to_ad


DEFAULT_IMPORT_PATH = Path(__file__).resolve().parents[2] / "data" / "fundamentals_import.csv"


def _to_float(value: Any) -> float | None:
    text = str(value or "").replace(",", "").strip()
    if not text or text in {"-", "--", "NA", "N/A", "null", "None"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _to_bool(value: Any) -> bool | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    if text in {"1", "true", "yes", "y", "是"}:
        return True
    if text in {"0", "false", "no", "n", "否"}:
        return False
    return None


def _to_year(value: Any) -> int | None:
    text = "".join(ch for ch in str(value or "") if ch.isdigit())
    if not text:
        return None
    try:
        year = int(text)
    except ValueError:
        return None
    return year + 1911 if year < 1911 else year


def _to_quarter(value: Any) -> int | None:
    text = str(value or "").upper().strip()
    if "Q" in text:
        text = text.rsplit("Q", 1)[-1]
    digits = "".join(ch for ch in text if ch.isdigit())
    if not digits:
        return None
    try:
        quarter = int(digits[-1])
    except ValueError:
        return None
    return quarter if 1 <= quarter <= 4 else None


def _to_month(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if len(text) >= 7 and text[4] in {"-", "/"}:
        return f"{text[:4]}-{text[5:7]}"
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) == 6 and int(digits[:4]) > 1911:
        return f"{digits[:4]}-{digits[4:6]}"
    converted = roc_month_to_ad(text)
    return converted or None


def _normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    return {str(key or "").strip().lstrip("\ufeff"): value for key, value in row.items() if key is not None}


def _pick(row: dict[str, Any], *keys: str) -> Any:
    lower_map = {key.lower(): value for key, value in row.items()}
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
        value = lower_map.get(key.lower())
        if value not in (None, ""):
            return value
    return None


def _has_any(*values: Any) -> bool:
    return any(value is not None for value in values)


@dataclass(frozen=True)
class ImportedMonthlyRevenueMetrics:
    stockCode: str
    month: str | None = None
    monthlyRevenueYoY: float | None = None
    previousMonthRevenueYoY: float | None = None
    cumulativeRevenueYoY: float | None = None
    trailingThreeMonthAverageYoY: float | None = None
    janFebCombinedRevenueYoY: float | None = None
    isSpringFestivalMonth: bool | None = None


@dataclass(frozen=True)
class ImportedQuarterlyFundamental:
    stockCode: str
    fiscalYear: int | None = None
    quarter: int | None = None
    eps: float | None = None
    epsYoY: float | None = None
    netIncome: float | None = None
    netIncomeYoY: float | None = None
    revenue: float | None = None
    grossMargin: float | None = None
    grossMarginYoY: float | None = None
    operatingMargin: float | None = None

    @property
    def period(self) -> str | None:
        if self.fiscalYear and self.quarter:
            return f"{self.fiscalYear}Q{self.quarter}"
        return None


@dataclass(frozen=True)
class ImportedValuation:
    stockCode: str
    per: float | None = None
    priceBookRatio: float | None = None
    dividendYield: float | None = None
    valuationDate: str | None = None
    valuationFiscalQuarter: str | None = None
    inventoryTurnover: float | None = None
    roe: float | None = None
    nonPerformingLoanRatio: float | None = None
    capitalAdequacyRatio: float | None = None
    netInterestMargin: float | None = None


@dataclass(frozen=True)
class ImportedAnnualFinancial:
    stockCode: str
    year: int
    netIncome: float | None = None


@dataclass(frozen=True)
class ImportedFundamentalBundle:
    monthly: dict[str, ImportedMonthlyRevenueMetrics] = field(default_factory=dict)
    quarterly: dict[str, ImportedQuarterlyFundamental] = field(default_factory=dict)
    valuations: dict[str, ImportedValuation] = field(default_factory=dict)
    annuals: dict[str, list[ImportedAnnualFinancial]] = field(default_factory=dict)
    status: dict[str, Any] = field(default_factory=dict)


def _quarter_key(row: ImportedQuarterlyFundamental) -> tuple[int, int]:
    return (row.fiscalYear or 0, row.quarter or 0)


def _monthly_key(row: ImportedMonthlyRevenueMetrics) -> str:
    return row.month or ""


class LocalFundamentalsImportAdapter:
    """Optional CSV import for fields not reliably available from official OpenAPI."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else DEFAULT_IMPORT_PATH

    def fetch_bundle(self) -> ImportedFundamentalBundle:
        if not self.path.exists():
            return ImportedFundamentalBundle(
                status={
                    "enabled": False,
                    "path": str(self.path),
                    "rows": 0,
                    "quarterlyRows": 0,
                    "annualRows": 0,
                    "monthlyRows": 0,
                    "valuationRows": 0,
                    "skippedRows": 0,
                }
            )

        monthly: dict[str, ImportedMonthlyRevenueMetrics] = {}
        quarterly: dict[str, ImportedQuarterlyFundamental] = {}
        valuations: dict[str, ImportedValuation] = {}
        annuals: dict[str, list[ImportedAnnualFinancial]] = {}
        rows = 0
        skipped = 0

        with self.path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for raw_row in reader:
                rows += 1
                row = _normalize_row(raw_row)
                stock_code = str(
                    _pick(row, "stock_code", "stockCode", "company_code", "公司代號", "SecuritiesCompanyCode") or ""
                ).strip()
                if not stock_code:
                    skipped += 1
                    continue

                monthly_row = self._parse_monthly(row, stock_code)
                if monthly_row and _monthly_key(monthly_row) >= _monthly_key(monthly.get(stock_code, monthly_row)):
                    monthly[stock_code] = monthly_row

                quarterly_row = self._parse_quarterly(row, stock_code)
                existing_quarterly = quarterly.get(stock_code)
                if quarterly_row and (existing_quarterly is None or _quarter_key(quarterly_row) >= _quarter_key(existing_quarterly)):
                    quarterly[stock_code] = quarterly_row

                valuation_row = self._parse_valuation(row, stock_code)
                if valuation_row:
                    valuations[stock_code] = valuation_row

                annual_row = self._parse_annual(row, stock_code)
                if annual_row:
                    annuals.setdefault(stock_code, []).append(annual_row)

        for stock_code, items in annuals.items():
            deduped = {item.year: item for item in items}
            annuals[stock_code] = [deduped[year] for year in sorted(deduped)]

        return ImportedFundamentalBundle(
            monthly=monthly,
            quarterly=quarterly,
            valuations=valuations,
            annuals=annuals,
            status={
                "enabled": True,
                "path": str(self.path),
                "rows": rows,
                "quarterlyRows": len(quarterly),
                "annualRows": sum(len(items) for items in annuals.values()),
                "monthlyRows": len(monthly),
                "valuationRows": len(valuations),
                "skippedRows": skipped,
            },
        )

    def _parse_monthly(self, row: dict[str, Any], stock_code: str) -> ImportedMonthlyRevenueMetrics | None:
        month = _to_month(_pick(row, "month", "data_month", "資料年月", "營收年月"))
        monthly_yoy = _to_float(_pick(row, "monthly_revenue_yoy", "monthlyRevenueYoY", "月營收年增率"))
        previous_yoy = _to_float(
            _pick(row, "previous_month_revenue_yoy", "previousMonthRevenueYoY", "上月營收年增率")
        )
        cumulative_yoy = _to_float(
            _pick(row, "cumulative_revenue_yoy", "cumulativeRevenueYoY", "累計營收年增率")
        )
        trailing_yoy = _to_float(
            _pick(row, "trailing_3m_average_yoy", "trailingThreeMonthAverageYoY", "近三月平均營收年增率")
        )
        jan_feb_yoy = _to_float(
            _pick(row, "jan_feb_combined_revenue_yoy", "janFebCombinedRevenueYoY", "一二月合併營收年增率")
        )
        spring = _to_bool(_pick(row, "is_spring_festival_month", "isSpringFestivalMonth", "春節月"))

        if not _has_any(month, monthly_yoy, previous_yoy, cumulative_yoy, trailing_yoy, jan_feb_yoy, spring):
            return None
        return ImportedMonthlyRevenueMetrics(
            stockCode=stock_code,
            month=month,
            monthlyRevenueYoY=monthly_yoy,
            previousMonthRevenueYoY=previous_yoy,
            cumulativeRevenueYoY=cumulative_yoy,
            trailingThreeMonthAverageYoY=trailing_yoy,
            janFebCombinedRevenueYoY=jan_feb_yoy,
            isSpringFestivalMonth=spring,
        )

    def _parse_quarterly(self, row: dict[str, Any], stock_code: str) -> ImportedQuarterlyFundamental | None:
        fiscal_year = _to_year(_pick(row, "fiscal_year", "year", "年度"))
        quarter = _to_quarter(_pick(row, "quarter", "season", "季別", "fiscal_quarter", "財報季度"))
        eps = _to_float(_pick(row, "eps", "EPS", "每股盈餘"))
        eps_yoy = _to_float(_pick(row, "eps_yoy", "epsYoY", "EPS年增率"))
        net_income = _to_float(_pick(row, "net_income", "netIncome", "淨利", "稅後淨利"))
        net_income_yoy = _to_float(_pick(row, "net_income_yoy", "netIncomeYoY", "淨利年增率"))
        revenue = _to_float(_pick(row, "revenue", "營業收入"))
        gross_margin = _to_float(_pick(row, "gross_margin", "grossMargin", "毛利率"))
        gross_margin_yoy = _to_float(_pick(row, "gross_margin_yoy", "grossMarginYoY", "毛利率年增率"))
        operating_margin = _to_float(_pick(row, "operating_margin", "operatingMargin", "營業利益率"))

        if not _has_any(
            fiscal_year,
            quarter,
            eps,
            eps_yoy,
            net_income,
            net_income_yoy,
            revenue,
            gross_margin,
            gross_margin_yoy,
            operating_margin,
        ):
            return None
        return ImportedQuarterlyFundamental(
            stockCode=stock_code,
            fiscalYear=fiscal_year,
            quarter=quarter,
            eps=eps,
            epsYoY=eps_yoy,
            netIncome=net_income,
            netIncomeYoY=net_income_yoy,
            revenue=revenue,
            grossMargin=gross_margin,
            grossMarginYoY=gross_margin_yoy,
            operatingMargin=operating_margin,
        )

    def _parse_valuation(self, row: dict[str, Any], stock_code: str) -> ImportedValuation | None:
        per = _to_float(_pick(row, "per", "PER", "本益比"))
        pbr = _to_float(_pick(row, "price_book_ratio", "priceBookRatio", "pbr", "PBR", "股價淨值比"))
        yield_ratio = _to_float(_pick(row, "dividend_yield", "dividendYield", "殖利率"))
        valuation_date = str(_pick(row, "valuation_date", "valuationDate", "估值日期") or "").strip() or None
        valuation_quarter = str(
            _pick(row, "valuation_fiscal_quarter", "valuationFiscalQuarter", "估值財報季度") or ""
        ).strip() or None
        inventory_turnover = _to_float(_pick(row, "inventory_turnover", "inventoryTurnover", "存貨週轉率"))
        roe = _to_float(_pick(row, "roe", "ROE", "權益報酬率"))
        npl = _to_float(_pick(row, "non_performing_loan_ratio", "nonPerformingLoanRatio", "逾放比"))
        capital_adequacy = _to_float(_pick(row, "capital_adequacy_ratio", "capitalAdequacyRatio", "資本適足率"))
        nim = _to_float(_pick(row, "net_interest_margin", "netInterestMargin", "利差", "淨利差"))

        if not _has_any(per, pbr, yield_ratio, valuation_date, valuation_quarter, inventory_turnover, roe, npl, capital_adequacy, nim):
            return None
        return ImportedValuation(
            stockCode=stock_code,
            per=per,
            priceBookRatio=pbr,
            dividendYield=yield_ratio,
            valuationDate=valuation_date,
            valuationFiscalQuarter=valuation_quarter,
            inventoryTurnover=inventory_turnover,
            roe=roe,
            nonPerformingLoanRatio=npl,
            capitalAdequacyRatio=capital_adequacy,
            netInterestMargin=nim,
        )

    def _parse_annual(self, row: dict[str, Any], stock_code: str) -> ImportedAnnualFinancial | None:
        year = _to_year(_pick(row, "annual_year", "年度淨利年度", "年報年度"))
        net_income = _to_float(_pick(row, "annual_net_income", "年度淨利", "年報淨利"))
        if year is None or net_income is None:
            return None
        return ImportedAnnualFinancial(stockCode=stock_code, year=year, netIncome=net_income)
