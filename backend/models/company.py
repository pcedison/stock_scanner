from typing import Literal

from pydantic import BaseModel, Field


class Company(BaseModel):
    stockCode: str = Field(..., pattern=r"^\d{4,6}$")
    name: str = Field(..., min_length=1)
    market: Literal["TWSE", "TPEX", "OTHER"]
    industryName: str = Field(..., min_length=1)
    isFinancial: bool = False
