"""The yearly TWSE holiday schedule is fetched automatically; bad data must never land."""

from __future__ import annotations

import json
from datetime import date

import pytest

import scripts.update_market_calendars as updater


def _calendar(year: int, closed: list[str], spring: list[str] | None = None) -> dict:
    return {
        "source": f"TWSE holiday schedule {year}",
        "sourceUrl": "https://www.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule",
        "year": year,
        "closedDates": closed,
        "springFestivalDates": spring if spring is not None else [f"{year}-02-08"],
    }


GOOD_2027 = _calendar(
    2027,
    ["2027-01-01", "2027-02-04", "2027-02-05", "2027-02-08", "2027-02-09", "2027-02-10", "2027-02-11",
     "2027-03-01", "2027-04-05", "2027-04-06", "2027-06-09", "2027-09-15", "2027-10-11"],
    ["2027-02-05", "2027-02-08", "2027-02-09"],
)


def test_an_unpublished_year_is_left_alone(tmp_path):
    result = updater.update_year(2027, lambda year: _calendar(year, [], []), tmp_path)

    assert result["status"] == "not_published"
    assert not (tmp_path / "market_calendar_2027.json").exists()


def test_a_published_year_is_written(tmp_path):
    result = updater.update_year(2027, lambda year: GOOD_2027, tmp_path)

    assert result["status"] == "created"
    saved = json.loads((tmp_path / "market_calendar_2027.json").read_text(encoding="utf-8"))
    assert saved["closedDates"] == GOOD_2027["closedDates"]


def test_an_identical_schedule_does_not_rewrite_the_file(tmp_path):
    path = tmp_path / "market_calendar_2027.json"
    path.write_text(json.dumps({**GOOD_2027, "source": "older wording"}), encoding="utf-8")

    result = updater.update_year(2027, lambda year: GOOD_2027, tmp_path)

    assert result["status"] == "unchanged"
    assert json.loads(path.read_text(encoding="utf-8"))["source"] == "older wording"


def test_spring_festival_wording_differences_within_the_same_month_are_not_a_change(tmp_path):
    # The committed 2026 file came from the HTML schedule and lists the Spring Festival days
    # slightly differently from the JSON API; only the month is used (Jan/Feb revenue rules).
    path = tmp_path / "market_calendar_2027.json"
    path.write_text(json.dumps(GOOD_2027), encoding="utf-8")
    reworded = {**GOOD_2027, "springFestivalDates": ["2027-02-04", "2027-02-06", "2027-02-12"]}

    assert updater.update_year(2027, lambda year: reworded, tmp_path)["status"] == "unchanged"


def test_a_revision_adding_a_holiday_is_applied(tmp_path):
    (tmp_path / "market_calendar_2027.json").write_text(json.dumps(GOOD_2027), encoding="utf-8")
    revised = _calendar(2027, [*GOOD_2027["closedDates"], "2027-12-31"], GOOD_2027["springFestivalDates"])

    assert updater.update_year(2027, lambda year: revised, tmp_path)["status"] == "updated"


@pytest.mark.parametrize(
    ("calendar", "problem"),
    [
        (_calendar(2027, GOOD_2027["closedDates"][1:]), "New Year's Day"),  # 2027-01-01 is a Friday
        (_calendar(2027, ["2027-01-01", "2027-02-08", "2027-02-09"]), "too few"),
        (_calendar(2027, [*GOOD_2027["closedDates"], "2026-12-31"]), "outside 2027"),
        (_calendar(2027, [*GOOD_2027["closedDates"], "2027-01-02"]), "weekend"),  # Saturday
        (_calendar(2027, GOOD_2027["closedDates"], []), "Spring Festival"),
    ],
)
def test_suspicious_schedules_are_rejected(tmp_path, calendar, problem):
    result = updater.update_year(2027, lambda year: calendar, tmp_path)

    assert result["status"] == "invalid"
    assert any(problem in item for item in result["problems"])
    assert not (tmp_path / "market_calendar_2027.json").exists()


def test_new_years_day_on_a_weekend_is_not_required():
    # 2028-01-01 is a Saturday; the parser keeps weekday closures only.
    closed = ["2028-01-24", "2028-01-25", "2028-01-26", "2028-01-27", "2028-01-28", "2028-02-28", "2028-04-04"]
    assert updater.validate_calendar(2028, _calendar(2028, closed, ["2028-01-26"])) == []


def test_a_revision_that_drops_many_holidays_is_rejected(tmp_path):
    (tmp_path / "market_calendar_2027.json").write_text(json.dumps(GOOD_2027), encoding="utf-8")
    partial = _calendar(2027, GOOD_2027["closedDates"][:-3], GOOD_2027["springFestivalDates"])

    result = updater.update_year(2027, lambda year: partial, tmp_path)

    assert result["status"] == "invalid"
    assert any("drop" in item for item in result["problems"])
    assert json.loads((tmp_path / "market_calendar_2027.json").read_text(encoding="utf-8")) == GOOD_2027


def test_a_fetch_error_is_reported_not_raised(tmp_path):
    def broken(year):
        raise RuntimeError("TWSE unreachable")

    result = updater.update_year(2027, broken, tmp_path)

    assert result["status"] == "fetch_failed"


def test_main_checks_this_year_and_next_and_writes_github_outputs(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(updater, "today_taipei", lambda: date(2026, 11, 2))
    fetched: list[int] = []

    def fetch(year):
        fetched.append(year)
        return GOOD_2027 if year == 2027 else _calendar(year, [], [])

    monkeypatch.setattr(updater, "fetch_twse_market_calendar", fetch)
    output = tmp_path / "gh_output.txt"

    rc = updater.main(["--data-dir", str(tmp_path), "--github-output", str(output)])

    assert rc == 0
    assert fetched == [2026, 2027]
    assert "changed=true" in output.read_text(encoding="utf-8")
    assert "years=2027" in output.read_text(encoding="utf-8")
    assert json.loads(capsys.readouterr().out)["changed"] == ["2027"]


def test_workflow_runs_every_year_through_the_publication_season():
    from pathlib import Path

    import yaml

    workflow = yaml.safe_load(Path(".github/workflows/update-market-calendar.yml").read_text(encoding="utf-8"))
    triggers = workflow.get("on", workflow.get(True))
    crons = [item["cron"] for item in triggers["schedule"]]

    # Weekly October-January, monthly the rest of the year: no year-specific dates to maintain.
    assert "17 1 * 10,11,12,1 1" in crons
    assert "17 1 1 2-9 *" in crons
    assert "workflow_dispatch" in triggers
    steps = "\n".join(str(step.get("run", "")) for step in workflow["jobs"]["update"]["steps"])
    assert "scripts/update_market_calendars.py" in steps
    assert "tests/test_filing_calendar.py" in steps


def test_main_fails_the_run_when_data_is_suspicious(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "today_taipei", lambda: date(2026, 11, 2))
    monkeypatch.setattr(updater, "fetch_twse_market_calendar", lambda year: _calendar(year, ["2027-01-01"]))

    assert updater.main(["--data-dir", str(tmp_path)]) == 1
