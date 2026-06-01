import pytest

import backend.services.calendar as calendar_module


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

    calendar = calendar_module._parse_twse_holiday_payload(2026, payload)

    assert "2026-02-11" not in calendar["closedDates"]
    assert "2026-02-12" in calendar["closedDates"]
    assert "2026-02-15" not in calendar["closedDates"]
    assert "2026-02-20" in calendar["closedDates"]
    assert "2026-02-23" not in calendar["closedDates"]
    assert "2026-02-15" in calendar["springFestivalDates"]


def test_parse_twse_holiday_payload_skips_blank_bad_and_foreign_year_rows():
    payload = {
        "fields": ["日期", "名稱", "說明"],
        "data": [
            {"日期": "", "名稱": "空白", "說明": ""},  # blank date -> skip
            {"日期": "not-a-date", "名稱": "壞", "說明": ""},  # unparseable -> skip
            {"日期": "2025-02-12", "名稱": "市場無交易", "說明": ""},  # other year -> skip
            {"日期": "2026-02-12", "名稱": "市場無交易", "說明": ""},  # valid, in-year
        ],
    }
    result = calendar_module._parse_twse_holiday_payload(2026, payload)
    assert result["closedDates"] == ["2026-02-12"]


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_fetch_twse_market_calendar_parses_ok_payload(monkeypatch):
    payload = {"stat": "OK", "fields": ["日期", "名稱", "說明"], "data": [["2026-02-12", "市場無交易", ""]]}
    monkeypatch.setattr(calendar_module.httpx, "get", lambda *a, **k: _FakeResponse(payload))
    result = calendar_module.fetch_twse_market_calendar(2026)
    assert result["closedDates"] == ["2026-02-12"]


def test_fetch_twse_market_calendar_rejects_invalid_payload(monkeypatch):
    monkeypatch.setattr(calendar_module.httpx, "get", lambda *a, **k: _FakeResponse({"stat": "error"}))
    with pytest.raises(ValueError, match="invalid payload"):
        calendar_module.fetch_twse_market_calendar(2026)


def test_market_calendar_load_falls_back_when_file_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(calendar_module, "ROOT_DIR", tmp_path)  # empty dir, no calendar file
    cal = calendar_module.MarketCalendar.load(2026)
    assert cal.source == "weekend-only fallback"
    assert cal.closed_dates == set()


def test_update_market_calendar_writes_then_reloads(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(calendar_module, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(
        calendar_module,
        "fetch_twse_market_calendar",
        lambda year, timeout=20: {
            "source": "test feed",
            "sourceUrl": "",
            "year": year,
            "closedDates": ["2026-02-12"],
            "springFestivalDates": ["2026-02-15"],
        },
    )
    cal = calendar_module.update_market_calendar(2026)
    assert (tmp_path / "data" / "market_calendar_2026.json").exists()
    assert cal.source == "test feed"
    assert date_in(cal.closed_dates, "2026-02-12")


def date_in(dates, iso):
    return any(d.isoformat() == iso for d in dates)
