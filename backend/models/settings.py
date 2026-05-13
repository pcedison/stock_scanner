from typing import Literal

from pydantic import BaseModel


class ScannerSettings(BaseModel):
    auto_scan_full_market: bool = True
    manual_scan_enabled: bool = True
    exclude_financial_industry: bool = True
    use_mock_data: bool = False
    scan_twse: bool = True
    scan_tpex: bool = True
    spring_festival_guard: bool = True
    revenue_growth_mode: Literal["cumulative_ytd", "monthly", "trailing_3m_avg"] = "cumulative_ytd"
