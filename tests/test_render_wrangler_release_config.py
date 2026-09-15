from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from scripts.render_wrangler_release_config import (
    PRODUCTION_CRONS,
    main,
    render_production_profile,
    render_staging_config,
)

PRODUCTION_D1 = "c29c7b4a-4a2d-4711-badd-1aaad43ce3b6"
PRODUCTION_BUCKET = "stock-scanner-beta-cache"
# Retired on 2026-09-15 together with the v1 market-scan routes; it must stay rejected.
RETIRED_ROLLBACK_PROFILE = "production-v1-rollback"
RETIRED_ROLLBACK_FLAG = "--confirm-production-v1-rollback"


def parsed(text: str):
    return tomllib.loads(text)


def _committed_production_vars() -> dict:
    return parsed(Path("cloudflare/wrangler.toml").read_text(encoding="utf-8"))["vars"]


def test_rendered_staging_config_is_complete_and_isolated():
    rendered = render_staging_config(
        template_path=Path("cloudflare/wrangler.staging.template.toml"),
        database_id="11111111-1111-1111-1111-111111111111",
        bucket_name="stock-scanner-beta-cache-staging-test",
        production_database_id=PRODUCTION_D1,
        production_bucket_name=PRODUCTION_BUCKET,
    )
    config = parsed(rendered)

    assert config["main"] == "worker.py"
    assert config["vars"]["APP_ENV"] == "staging"
    assert config["vars"]["GITHUB_DISPATCH_ENABLED"] == "false"
    assert config["vars"]["EDGE_CACHE_ENABLED"] == "true"
    assert config["cache"]["enabled"] is True
    assert config["triggers"]["crons"] == []
    assert config["d1_databases"][0]["database_id"] != PRODUCTION_D1
    assert config["r2_buckets"][0]["bucket_name"] != PRODUCTION_BUCKET
    assert "__STAGING" not in rendered


def test_staging_config_rejects_production_resources():
    with pytest.raises(ValueError, match="production database"):
        render_staging_config(
            database_id=PRODUCTION_D1,
            bucket_name="stock-scanner-beta-cache-staging-test",
            production_database_id=PRODUCTION_D1,
            production_bucket_name=PRODUCTION_BUCKET,
        )
    with pytest.raises(ValueError, match="production bucket"):
        render_staging_config(
            database_id="11111111-1111-1111-1111-111111111111",
            bucket_name=PRODUCTION_BUCKET,
            production_database_id=PRODUCTION_D1,
            production_bucket_name=PRODUCTION_BUCKET,
        )


def test_production_cron_profile_enables_cron_and_dispatch_atomically():
    config = parsed(render_production_profile("production-cron", enable_production_cron=True))

    assert config["triggers"]["crons"] == PRODUCTION_CRONS
    assert config["vars"]["GITHUB_DISPATCH_ENABLED"] == "true"
    assert set(config["vars"]) == set(_committed_production_vars())
    assert config["vars"]["EDGE_CACHE_ENABLED"] == "false"
    assert config["cache"]["enabled"] is False


def test_production_v2_profile_changes_only_release_switches():
    base = parsed(Path("cloudflare/wrangler.toml").read_text(encoding="utf-8"))
    v2 = parsed(
        render_production_profile(
            "production-v2",
            enable_production_cron=True,
            enable_production_v2=True,
        )
    )
    assert v2["triggers"]["crons"] == PRODUCTION_CRONS
    assert v2["vars"]["GITHUB_DISPATCH_ENABLED"] == "true"
    # v1 is retired, so the renderer no longer introduces a market API switch.
    assert set(v2["vars"]) == set(base["vars"])
    assert v2["vars"]["EDGE_CACHE_ENABLED"] == "true"
    assert v2["cache"]["enabled"] is True
    assert v2["d1_databases"] == base["d1_databases"]
    assert v2["r2_buckets"] == base["r2_buckets"]


def test_the_retired_market_rollback_profile_is_rejected(tmp_path):
    # The only remaining rollback is `wrangler rollback`; no config profile restores v1.
    with pytest.raises(ValueError, match="unsupported production profile"):
        render_production_profile(RETIRED_ROLLBACK_PROFILE, enable_production_cron=True)

    # The CLI must refuse it too, at argparse level, before any config is written.
    output = tmp_path / f"wrangler.{RETIRED_ROLLBACK_PROFILE}.generated.toml"
    with pytest.raises(SystemExit) as caught:
        main([
            RETIRED_ROLLBACK_PROFILE,
            "--enable-production-cron",
            RETIRED_ROLLBACK_FLAG,
            "--output",
            str(output),
        ])
    assert caught.value.code != 0
    assert not output.exists()


def test_production_profiles_are_fail_closed_without_release_acknowledgements():
    with pytest.raises(ValueError, match="enable-production-cron"):
        render_production_profile("production-cron")
    with pytest.raises(ValueError, match="enable-production-v2"):
        render_production_profile("production-v2", enable_production_cron=True)
    with pytest.raises(ValueError, match="only --enable-production-cron"):
        render_production_profile("production-cron", enable_production_cron=True, enable_production_v2=True)
