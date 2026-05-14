from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from .company import Company


AnalysisStatus = Literal["ENTRY", "WATCH", "HOLD", "ADD_WATCH", "WARNING", "EXIT", "EXCLUDED", "INSUFFICIENT_DATA"]


class RuleResult(BaseModel):
    code: str
    title: str
    passed: bool
    severity: Literal["INFO", "WATCH", "WARNING", "EXIT", "EXCLUDED", "INSUFFICIENT_DATA"] = "INFO"
    message: str
    evidence: Optional[list[dict[str, Any]]] = None


class AnalysisResult(BaseModel):
    stockCode: str
    companyName: str
    status: AnalysisStatus
    summary: str
    reasons: list[RuleResult] = Field(..., min_length=1)
    company: Optional[Company] = None
