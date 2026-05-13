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
    epsYoY: Optional[float]
    netIncomeYoY: Optional[float]
    grossMarginYoY: Optional[float] = None


class Valuation(BaseModel):
    per: Optional[float]
    inventoryTurnover: Optional[float]


class AnnualFinancial(BaseModel):
    year: int
    netIncome: Optional[float] = None


class FundamentalSnapshot(BaseModel):
    company: Company
    monthlyRevenue: MonthlyRevenue
    quarterlyFinancial: QuarterlyFinancial
    valuation: Valuation
    annualFinancials: list[AnnualFinancial] = Field(default_factory=list)
