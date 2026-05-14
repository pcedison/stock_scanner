from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx


ROOT_DIR = Path(__file__).resolve().parents[2]
TWSE_HOLIDAY_URL = "https://www.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule"


@dataclass(frozen=True)
class MarketCalendar:
    year: int
    closed_dates: set[date]
    spring_festival_dates: set[date]
    source: str
    source_url: str

    @classmethod
    def load(cls, year: int) -> "MarketCalendar":
        path = ROOT_DIR / "data" / f"market_calendar_{year}.json"
        if not path.exists():
            return cls(year=year, closed_dates=set(), spring_festival_dates=set(), source="weekend-only fallback", source_url="")

        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            year=year,
            closed_dates={date.fromisoformat(value) for value in raw.get("closedDates", [])},
            spring_festival_dates={date.fromisoformat(value) for value in raw.get("springFestivalDates", [])},
            source=raw.get("source", "local market calendar"),
            source_url=raw.get("sourceUrl", ""),
        )

    def is_trading_day(self, day: date) -> bool:
        return day.weekday() < 5 and day not in self.closed_dates

    def next_trading_day(self, day: date) -> date:
        cursor = day
        while not self.is_trading_day(cursor):
            cursor += timedelta(days=1)
        return cursor

    def is_spring_festival_month(self, month: str) -> bool:
        return any(day.strftime("%Y-%m") == month for day in self.spring_festival_dates)


def load_market_calendar(year: int) -> MarketCalendar:
    return MarketCalendar.load(year)


def _is_closed_row(name: str, description: str) -> bool:
    text = f"{name} {description}"
    if "開始交易" in text or "最後交易" in text:
        return False
    return any(keyword in text for keyword in ["放假", "無交易", "補假", "春節", "除夕"])


def _parse_twse_holiday_payload(year: int, payload: dict[str, Any]) -> dict[str, Any]:
    fields = payload.get("fields") or []
    rows = payload.get("data") or []
    closed_dates: set[str] = set()
    spring_dates: set[str] = set()

    for item in rows:
        row = dict(zip(fields, item)) if isinstance(item, list) else item
        day_text = str(row.get("日期") or "").strip()
        if not day_text:
            continue
        try:
            day = date.fromisoformat(day_text)
        except ValueError:
            continue
        if day.year != year:
            continue

        name = str(row.get("名稱") or "").strip()
        description = str(row.get("說明") or "").strip()
        if _is_closed_row(name, description) and day.weekday() < 5:
            closed_dates.add(day.isoformat())
        if any(keyword in f"{name} {description}" for keyword in ["春節", "除夕"]):
            spring_dates.add(day.isoformat())

    return {
        "source": f"TWSE holiday schedule {year}",
        "sourceUrl": f"{TWSE_HOLIDAY_URL}?response=json&queryYear={year}",
        "year": year,
        "closedDates": sorted(closed_dates),
        "springFestivalDates": sorted(spring_dates),
    }


def fetch_twse_market_calendar(year: int, timeout: float = 20) -> dict[str, Any]:
    params = {"response": "json", "queryYear": year}
    try:
        response = httpx.get(TWSE_HOLIDAY_URL, params=params, timeout=timeout, follow_redirects=True)
    except httpx.TransportError:
        response = httpx.get(TWSE_HOLIDAY_URL, params=params, timeout=timeout, follow_redirects=True, verify=False)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or str(payload.get("stat", "")).lower() != "ok":
        raise ValueError("TWSE holiday schedule returned an invalid payload")
    return _parse_twse_holiday_payload(year, payload)


def update_market_calendar(year: int, timeout: float = 20) -> MarketCalendar:
    calendar = fetch_twse_market_calendar(year, timeout=timeout)
    path = ROOT_DIR / "data" / f"market_calendar_{year}.json"
    path.write_text(json.dumps(calendar, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return MarketCalendar.load(year)
