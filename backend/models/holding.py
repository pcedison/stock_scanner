from __future__ import annotations

from pydantic import BaseModel, Field


class Holding(BaseModel):
    stockCode: str = Field(..., pattern=r"^\d{4,6}$")
    name: str | None = None
    shares: int = Field(default=0, ge=0)
    averageCost: float | None = Field(default=None, ge=0)
