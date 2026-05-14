from backend.services.calendar import _parse_twse_holiday_payload


def test_parse_twse_holiday_payload_keeps_trading_markers_open():
    payload = {
        "stat": "ok",
        "fields": ["日期", "名稱", "說明"],
        "data": [
            ["2026-02-11", "農曆春節前最後交易日", "農曆春節前最後交易。"],
            ["2026-02-12", "市場無交易，僅辦理結算交割作業", ""],
            ["2026-02-15", "農曆除夕及春節", "依規定放假5日。"],
            ["2026-02-20", "農曆除夕及春節", "補假。"],
            ["2026-02-23", "農曆春節後開始交易日", "農曆春節後開始交易。"],
        ],
    }

    calendar = _parse_twse_holiday_payload(2026, payload)

    assert "2026-02-11" not in calendar["closedDates"]
    assert "2026-02-12" in calendar["closedDates"]
    assert "2026-02-15" not in calendar["closedDates"]
    assert "2026-02-20" in calendar["closedDates"]
    assert "2026-02-23" not in calendar["closedDates"]
    assert "2026-02-15" in calendar["springFestivalDates"]
