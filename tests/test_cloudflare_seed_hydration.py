from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

import scripts.hydrate_cloudflare_seed_inputs as hydration


def _history(updated_at: str, months: dict[str, dict[str, dict]]) -> dict:
    return {"schemaVersion": 1, "updatedAt": updated_at, "months": months}


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_seed_zip(path: Path, monthly_history: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("official_fundamentals_history.json", json.dumps({"quarters": {"1101": {"2026Q1": {}}}}))
        archive.writestr("official_history_backfill_progress.json", json.dumps({"status": "complete"}))
        archive.writestr("monthly_revenue_history.json", json.dumps(monthly_history))


def test_hydration_merges_committed_checkout_zip_and_r2_history(tmp_path):
    data_dir = tmp_path / "data"
    _write_json(
        data_dir / "monthly_revenue_history.json",
        _history("2026-07-12T00:00:00+00:00", {"1101": {"2026-06": {"monthlyRevenueYoY": 30.0}}}),
    )
    _write_seed_zip(
        data_dir / "official_cache_seed_2026-05-14.zip",
        _history("2026-05-25T00:00:00+00:00", {"1101": {"2026-04": {"monthlyRevenueYoY": 10.0}}}),
    )
    r2_history = tmp_path / "r2-monthly.json"
    _write_json(
        r2_history,
        _history("2026-06-22T00:00:00+00:00", {"1101": {"2026-05": {"monthlyRevenueYoY": 20.0}}}),
    )

    summary = hydration.hydrate_seed_inputs(data_dir, r2_history, min_consecutive_companies=1)

    merged = json.loads((data_dir / "monthly_revenue_history.json").read_text(encoding="utf-8"))
    assert list(merged["months"]["1101"]) == ["2026-04", "2026-05", "2026-06"]
    assert merged["months"]["1101"]["2026-05"]["monthlyRevenueYoY"] == 20.0
    assert merged["months"]["1101"]["2026-06"]["monthlyRevenueYoY"] == 30.0
    assert summary["latestMonth"] == "2026-06"
    assert summary["previousMonth"] == "2026-05"
    assert summary["consecutiveCompanies"] == 1
    assert json.loads((data_dir / "official_fundamentals_history.json").read_text(encoding="utf-8"))["quarters"]
    assert (data_dir / "official_history_backfill_progress.json").is_file()


def test_hydration_ignores_invalid_optional_r2_candidate(tmp_path):
    data_dir = tmp_path / "data"
    _write_json(
        data_dir / "monthly_revenue_history.json",
        _history(
            "2026-07-12T00:00:00+00:00",
            {"1101": {"2026-05": {"monthlyRevenueYoY": 20.0}, "2026-06": {"monthlyRevenueYoY": 30.0}}},
        ),
    )
    _write_seed_zip(
        data_dir / "official_cache_seed_2026-05-14.zip",
        _history("2026-05-25T00:00:00+00:00", {"1101": {"2026-04": {"monthlyRevenueYoY": 10.0}}}),
    )
    invalid = tmp_path / "invalid.json"
    invalid.write_text("not json", encoding="utf-8")

    summary = hydration.hydrate_seed_inputs(data_dir, invalid, min_consecutive_companies=1)

    assert summary["consecutiveCompanies"] == 1
    assert summary["ignoredOptionalCandidates"] == 1


def test_hydration_rejects_non_consecutive_latest_history_without_clobbering_checkout(tmp_path):
    data_dir = tmp_path / "data"
    checkout = _history("2026-07-12T00:00:00+00:00", {"1101": {"2026-06": {"monthlyRevenueYoY": 30.0}}})
    _write_json(data_dir / "monthly_revenue_history.json", checkout)
    _write_seed_zip(
        data_dir / "official_cache_seed_2026-05-14.zip",
        _history("2026-05-25T00:00:00+00:00", {"1101": {"2026-04": {"monthlyRevenueYoY": 10.0}}}),
    )

    with pytest.raises(hydration.HistoryCoverageError, match="consecutive monthly history"):
        hydration.hydrate_seed_inputs(data_dir, None, min_consecutive_companies=1)

    assert json.loads((data_dir / "monthly_revenue_history.json").read_text(encoding="utf-8")) == checkout
