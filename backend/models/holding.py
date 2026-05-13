from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class Holding(BaseModel):
    stockCode: str = Field(..., pattern=r"^\d{4,6}$")
    name: Optional[str] = None
    shares: int = Field(default=0, ge=0)
    averageCost: Optional[float] = Field(default=None, ge=0)
