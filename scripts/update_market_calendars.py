"""Fetch the TWSE holiday schedule for this year and next into data/market_calendar_<year>.json.

Run by .github/workflows/update-market-calendar.yml. TWSE publishes the next year's schedule
late in the year; until then it returns an empty list and nothing is written. The closed
dates drive the refresh cadence (publication slots, filing-deadline extension) and reach the
Worker through the seed manifest, so a wrong list silently skips or adds trading days - every
fetched schedule is validated before it can replace a file.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backend.services.calendar import fetch_twse_market_calendar  # noqa: E402

DATA_DIR = ROOT_DIR / "data"
# Taiwan closes roughly 15-19 weekdays a year; far fewer means a partial response.
MIN_CLOSED_DAYS = 6
# A revision may legitimately move a holiday or two; dropping more looks like a truncated list.
MAX_DROPPED_DAYS = 2


def today_taipei() -> date:
    return datetime.now(ZoneInfo("Asia/Taipei")).date()


def _dates(values: Any) -> list[date]:
    return [date.fromisoformat(str(value)) for value in values or []]


def _months(values: Any) -> set[str]:
    return {str(value)[:7] for value in values or []}


def validate_calendar(year: int, calendar: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    try:
        closed = _dates(calendar.get("closedDates"))
        spring = _dates(calendar.get("springFestivalDates"))
    except ValueError as exc:
        return [f"unparseable date: {exc}"]
    outside = sorted({day.isoformat() for day in [*closed, *spring] if day.year != year})
    if outside:
        problems.append(f"dates outside {year}: {', '.join(outside)}")
    weekends = [day.isoformat() for day in closed if day.weekday() >= 5]
    if weekends:
        problems.append(f"weekend dates listed as closures: {', '.join(weekends)}")
    if len(closed) < MIN_CLOSED_DAYS:
        problems.append(f"too few closed days ({len(closed)} < {MIN_CLOSED_DAYS})")
    new_year = date(year, 1, 1)
    if new_year.weekday() < 5 and new_year not in closed:
        problems.append("New Year's Day is missing")
    if not spring or any(day.month > 2 for day in spring):
        problems.append("Spring Festival dates are missing or not in January/February")
    return problems


def update_year(year: int, fetch: Callable[[int], dict[str, Any]], data_dir: Path) -> dict[str, Any]:
    path = data_dir / f"market_calendar_{year}.json"
    try:
        fetched = fetch(year)
    except Exception as exc:  # network or payload error: report, let the run fail
        return {"year": year, "status": "fetch_failed", "problems": [f"{type(exc).__name__}: {exc}"]}
    if not fetched.get("closedDates") and not fetched.get("springFestivalDates"):
        return {"year": year, "status": "not_published", "problems": []}
    problems = validate_calendar(year, fetched)

    existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    if existing is not None:
        # Consumers only use the Spring Festival *month*, and the HTML and JSON schedules word
        # those rows differently, so compare months to avoid rewriting an identical calendar.
        same = sorted(existing.get("closedDates", [])) == sorted(fetched.get("closedDates", [])) and _months(
            existing.get("springFestivalDates")
        ) == _months(fetched.get("springFestivalDates"))
        if same and not problems:
            return {"year": year, "status": "unchanged", "problems": []}
        dropped = sorted(set(existing.get("closedDates", [])) - set(fetched.get("closedDates", [])))
        if len(dropped) > MAX_DROPPED_DAYS:
            problems.append(f"revision would drop {len(dropped)} closed days: {', '.join(dropped)}")
    if problems:
        return {"year": year, "status": "invalid", "problems": problems}

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fetched, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "year": year,
        "status": "updated" if existing is not None else "created",
        "problems": [],
        "closedDays": len(fetched.get("closedDates", [])),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args(argv)

    current = today_taipei().year
    results = [update_year(year, fetch_twse_market_calendar, args.data_dir) for year in (current, current + 1)]
    changed = [str(item["year"]) for item in results if item["status"] in {"created", "updated"}]
    failed = [item for item in results if item["status"] in {"invalid", "fetch_failed"}]
    print(json.dumps({"results": results, "changed": changed}, ensure_ascii=False, indent=2))
    if args.github_output is not None:
        with args.github_output.open("a", encoding="utf-8") as handle:
            handle.write(f"changed={'true' if changed else 'false'}\n")
            handle.write(f"years={' '.join(changed)}\n")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
