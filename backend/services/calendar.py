from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[2]


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
