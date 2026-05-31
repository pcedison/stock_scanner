from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

import scripts.r2_refresh_decision as r2
from scripts.cloudflare_seed_upload_plan import build_upload_plan
from scripts.r2_refresh_decision import early_refresh_decision, find_count


def test_r2_refresh_decision_reads_cloudflare_and_wrangler_count_shapes():
    cloudflare_payload = {"result": [{"results": [{"cnt": 3}]}]}
    wrangler_payload = [{"results": [{"pending_count": 2}]}]

    assert find_count(cloudflare_payload) == 3
    assert find_count(wrangler_payload) == 2


def test_r2_refresh_decision_treats_stale_health_as_refresh_trigger():
    decision = early_refresh_decision(
        force=False,
        pending_count=0,
        health_payload={"cache": {"sourceLastCheckedAt": "2026-05-20T00:00:00+00:00"}},
        health_error="",
        health_url_configured=True,
        max_cache_age_hours=36,
        now=datetime(2026, 5, 22, 0, 0, tzinfo=UTC),
    )

    assert decision["runRefresh"] is True
    assert decision["staleRefresh"] is True
    assert decision["cacheAgeHours"] == 48


def test_r2_refresh_decision_force_bypasses_remote_checks():
    decision = early_refresh_decision(
        force=True,
        pending_count=0,
        health_payload=None,
        health_error="",
        health_url_configured=False,
        max_cache_age_hours=36,
    )

    assert decision["runRefresh"] is True
    assert decision["pendingCount"] == "manual"


def test_cloudflare_seed_upload_plan_covers_public_shards_official_and_seed_zip(tmp_path):
    seed_dir = tmp_path / "cloudflare" / "seed"
    data_dir = tmp_path / "data"
    (seed_dir / "analysis_shards").mkdir(parents=True)
    (seed_dir / "holding_analysis_shards").mkdir(parents=True)
    data_dir.mkdir()
    for name in (
        "manifest.json",
        "companies.json",
        "data_sources_status.json",
        "market_scan_latest.json",
        "market_scan_summary.json",
        "analysis_by_code.json",
        "holding_analysis_by_code.json",
    ):
        (seed_dir / name).write_text("{}", encoding="utf-8")
    (seed_dir / "analysis_shards" / "23.json").write_text("{}", encoding="utf-8")
    (seed_dir / "holding_analysis_shards" / "23.json").write_text("{}", encoding="utf-8")
    (data_dir / "official_fundamentals_history.json").write_text("{}", encoding="utf-8")
    (data_dir / "official_history_backfill_progress.json").write_text("{}", encoding="utf-8")
    (data_dir / "monthly_revenue_history.json").write_text("{}", encoding="utf-8")
    (data_dir / "official_cache_seed_2026-05-14.zip").write_text("zip", encoding="utf-8")

    plan = build_upload_plan(seed_dir, data_dir)
    keys = [item.object_key for item in plan]

    assert "public/manifest.json" in keys
    assert "public/market_scan_summary.json" in keys
    assert "public/analysis_shards/23.json" in keys
    assert "public/holding_analysis_shards/23.json" in keys
    assert "official/monthly_revenue_history.json" in keys
    assert "official/official_cache_seed_2026-05-14.zip" in keys
    assert json.loads(json.dumps([item.__dict__ for item in plan]))[0]["object_key"] == "public/manifest.json"


def test_find_count_searches_nested_shapes_and_rejects_non_integers():
    assert r2.find_count({"a": {"b": {"cnt": 5}}}) == 5
    assert r2.find_count([{"x": 1}, {"pending_count": 7}]) == 7
    assert r2.find_count({"nothing": "here"}) == 0
    assert r2.find_count_or_none({"nope": 1}, ("cnt",)) is None
    with pytest.raises(ValueError, match="cnt is not an integer"):
        r2.find_count({"cnt": "abc"})


def test_parse_timestamp_normalizes_to_utc_or_returns_none():
    assert r2.parse_timestamp("") is None
    assert r2.parse_timestamp(123) is None
    assert r2.parse_timestamp("not-a-date") is None
    naive = r2.parse_timestamp("2026-05-20T00:00:00")
    assert naive.utcoffset() == timedelta(0)
    aware = r2.parse_timestamp("2026-05-20T08:00:00+08:00")
    assert aware.utcoffset() == timedelta(0)
    assert aware.hour == 0


def test_health_age_hours_handles_missing_and_naive_now():
    assert r2.health_age_hours({}) is None
    assert r2.health_age_hours({"cache": {}}) is None
    via_generated = r2.health_age_hours(
        {"cache": {"generatedAt": "2026-05-20T00:00:00+00:00"}},
        now=datetime(2026, 5, 20, 6, 0, tzinfo=UTC),
    )
    assert via_generated == 6
    via_naive_now = r2.health_age_hours(
        {"cache": {"sourceLastCheckedAt": "2026-05-20T00:00:00+00:00"}},
        now=datetime(2026, 5, 20, 6, 0),
    )
    assert via_naive_now == 6


def test_early_refresh_decision_branch_matrix():
    on_error = early_refresh_decision(
        force=False, pending_count=0, health_payload=None, health_error="boom",
        health_url_configured=True, max_cache_age_hours=36,
    )
    assert on_error["staleRefresh"] is True and on_error["runRefresh"] is True

    missing_ts = early_refresh_decision(
        force=False, pending_count=0, health_payload={"cache": {}}, health_error="",
        health_url_configured=True, max_cache_age_hours=36,
    )
    assert missing_ts["staleRefresh"] is True

    fresh = early_refresh_decision(
        force=False, pending_count=0,
        health_payload={"cache": {"sourceLastCheckedAt": "2026-05-22T00:00:00+00:00"}},
        health_error="", health_url_configured=True, max_cache_age_hours=36,
        now=datetime(2026, 5, 22, 1, 0, tzinfo=UTC),
    )
    assert fresh["staleRefresh"] is False and fresh["runRefresh"] is False
    assert any("fresh" in message for message in fresh["messages"])

    configured_no_payload = early_refresh_decision(
        force=False, pending_count=0, health_payload=None, health_error="",
        health_url_configured=True, max_cache_age_hours=36,
    )
    assert configured_no_payload["staleRefresh"] is True

    pending_only = early_refresh_decision(
        force=False, pending_count=2, health_payload=None, health_error="",
        health_url_configured=False, max_cache_age_hours=36,
    )
    assert pending_only["runRefresh"] is True and pending_only["staleRefresh"] is False


def test_load_json_helpers_and_errors(tmp_path):
    assert r2.load_json_file(None) is None
    assert r2.load_json_file(tmp_path / "missing.json") is None
    with pytest.raises(ValueError, match="not valid JSON"):
        r2.load_json_value("{bad")


def test_main_pending_count_via_inline_json_and_file(capsys, tmp_path):
    assert r2.main(["pending-count", "--json", '{"pending_count": 9}']) == 0
    assert capsys.readouterr().out.strip() == "9"

    api_file = tmp_path / "api.json"
    api_file.write_text('{"result": [{"results": [{"cnt": 4}]}]}', encoding="utf-8")
    assert r2.main(["pending-count", "--json-file", str(api_file), "--fields", "cnt"]) == 0
    assert capsys.readouterr().out.strip() == "4"


def test_main_early_check_writes_github_outputs(tmp_path, capsys):
    output = tmp_path / "out.txt"
    summary = tmp_path / "summary.md"
    health = tmp_path / "health.json"
    health.write_text('{"cache": {"sourceLastCheckedAt": "2020-01-01T00:00:00+00:00"}}', encoding="utf-8")

    rc = r2.main([
        "early-check",
        "--health-json-file", str(health),
        "--health-url-configured",
        "--github-output", str(output),
        "--github-step-summary", str(summary),
        "--max-cache-age-hours", "1",
    ])

    assert rc == 0
    output_text = output.read_text(encoding="utf-8")
    assert "run_refresh=true" in output_text
    assert "stale_refresh=true" in output_text
    assert "stale" in summary.read_text(encoding="utf-8").lower()


def test_main_reports_invalid_json_as_failure(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{nope", encoding="utf-8")
    assert r2.main(["pending-count", "--json-file", str(bad)]) == 1


def test_find_count_or_none_rejects_non_integer_field():
    with pytest.raises(ValueError, match="cnt is not an integer"):
        r2.find_count_or_none({"cnt": "abc"}, ("cnt",))


def test_write_github_outputs_tolerates_missing_paths():
    # No output/summary paths configured (local runs); must be a silent no-op.
    r2.write_github_outputs(
        {"pendingCount": 0, "staleRefresh": False, "runRefresh": False, "messages": ["noted"]},
        None,
        None,
    )
