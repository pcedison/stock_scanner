from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from .company import Company


class MonthlyRevenue(BaseModel):
    month: str = Field(..., min_length=7, max_length=7)
    monthlyRevenueYoY: Optional[float]
    previousMonthRevenueYoY: Optional[float] = None
    cumulativeRevenueYoY: Optional[float]
    trailingThreeMonthAverageYoY: Optional[float] = None
    janFebCombinedRevenueYoY: Optional[float] = None
    isSpringFestivalMonth: bool = False


class QuarterlyFinancial(BaseModel):
    quarter: str = Field(..., min_length=6)
    eps: Optional[float] = None
    epsYoY: Optional[float]
    netIncome: Optional[float] = None
    netIncomeYoY: Optional[float]
    revenue: Optional[float] = None
    grossMargin: Optional[float] = None
    grossMarginYoY: Optional[float] = None
    operatingMargin: Optional[float] = None


class Valuation(BaseModel):
    per: Optional[float]
    priceBookRatio: Optional[float] = None
    dividendYield: Optional[float] = None
    valuationDate: Optional[str] = None
    valuationFiscalQuarter: Optional[str] = None
    inventoryTurnover: Optional[float]
    roe: Optional[float] = None
    nonPerformingLoanRatio: Optional[float] = None
    capitalAdequacyRatio: Optional[float] = None
    netInterestMargin: Optional[float] = None


class AnnualFinancial(BaseModel):
    year: int
    netIncome: Optional[float] = None


class FundamentalSnapshot(BaseModel):
    company: Company
    monthlyRevenue: MonthlyRevenue
    quarterlyFinancial: QuarterlyFinancial
    valuation: Valuation
    annualFinancials: list[AnnualFinancial] = Field(default_factory=list)
