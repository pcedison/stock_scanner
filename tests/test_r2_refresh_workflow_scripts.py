from __future__ import annotations

import json
from datetime import UTC, datetime

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
