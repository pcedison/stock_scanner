from __future__ import annotations

from pydantic import BaseModel, Field

from .company import Company


class MonthlyRevenue(BaseModel):
    month: str = Field(..., min_length=7, max_length=7)
    monthlyRevenueYoY: float | None
    previousMonthRevenueYoY: float | None = None
    cumulativeRevenueYoY: float | None
    trailingThreeMonthAverageYoY: float | None = None
    janFebCombinedRevenueYoY: float | None = None
    isSpringFestivalMonth: bool = False


class QuarterlyFinancial(BaseModel):
    quarter: str = Field(..., min_length=6)
    eps: float | None = None
    epsYoY: float | None
    netIncome: float | None = None
    netIncomeYoY: float | None
    revenue: float | None = None
    grossMargin: float | None = None
    grossMarginYoY: float | None = None
    operatingMargin: float | None = None


class Valuation(BaseModel):
    per: float | None
    priceBookRatio: float | None = None
    dividendYield: float | None = None
    valuationDate: str | None = None
    valuationFiscalQuarter: str | None = None
    inventoryTurnover: float | None
    roe: float | None = None
    nonPerformingLoanRatio: float | None = None
    capitalAdequacyRatio: float | None = None
    netInterestMargin: float | None = None


class AnnualFinancial(BaseModel):
    year: int
    netIncome: float | None = None


class FundamentalSnapshot(BaseModel):
    company: Company
    monthlyRevenue: MonthlyRevenue
    quarterlyFinancial: QuarterlyFinancial
    valuation: Valuation
    annualFinancials: list[AnnualFinancial] = Field(default_factory=list)
