from typing import Literal

from pydantic import BaseModel, StrictBool


class ScannerSettings(BaseModel):
    auto_scan_full_market: StrictBool = True
    manual_scan_enabled: StrictBool = True
    exclude_financial_industry: StrictBool = True
    use_mock_data: StrictBool = False
    scan_twse: StrictBool = True
    scan_tpex: StrictBool = True
    spring_festival_guard: StrictBool = True
    revenue_growth_mode: Literal["cumulative_ytd", "monthly", "trailing_3m_avg"] = "cumulative_ytd"
