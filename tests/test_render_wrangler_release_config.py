from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from scripts.render_wrangler_release_config import (
    PRODUCTION_CRON,
    render_production_profile,
    render_staging_config,
)

PRODUCTION_D1 = "c29c7b4a-4a2d-4711-badd-1aaad43ce3b6"
PRODUCTION_BUCKET = "stock-scanner-beta-cache"


def parsed(text: str):
    return tomllib.loads(text)


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
    assert config["vars"]["MARKET_SCAN_API_VERSION"] == "v2"
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

    assert config["triggers"]["crons"] == [PRODUCTION_CRON]
    assert config["vars"]["GITHUB_DISPATCH_ENABLED"] == "true"
    assert config["vars"]["MARKET_SCAN_API_VERSION"] == "v1"
    assert config["vars"]["EDGE_CACHE_ENABLED"] == "false"
    assert config["cache"]["enabled"] is False


def test_production_v2_and_rollback_profiles_change_only_release_switches():
    v2 = parsed(
        render_production_profile(
            "production-v2",
            enable_production_cron=True,
            enable_production_v2=True,
        )
    )
    assert v2["triggers"]["crons"] == [PRODUCTION_CRON]
    assert v2["vars"]["GITHUB_DISPATCH_ENABLED"] == "true"
    assert v2["vars"]["MARKET_SCAN_API_VERSION"] == "v2"
    assert v2["vars"]["EDGE_CACHE_ENABLED"] == "true"
    assert v2["cache"]["enabled"] is True

    rollback = parsed(
        render_production_profile(
            "production-v1-rollback",
            enable_production_cron=True,
            confirm_production_v1_rollback=True,
        )
    )
    assert rollback["triggers"]["crons"] == [PRODUCTION_CRON]
    assert rollback["vars"]["GITHUB_DISPATCH_ENABLED"] == "true"
    assert rollback["vars"]["MARKET_SCAN_API_VERSION"] == "v1"
    assert rollback["vars"]["EDGE_CACHE_ENABLED"] == "false"
    assert rollback["cache"]["enabled"] is False
    assert rollback["d1_databases"] == v2["d1_databases"]
    assert rollback["r2_buckets"] == v2["r2_buckets"]


def test_production_profiles_are_fail_closed_without_release_acknowledgements():
    with pytest.raises(ValueError, match="enable-production-cron"):
        render_production_profile("production-cron")
    with pytest.raises(ValueError, match="enable-production-v2"):
        render_production_profile("production-v2", enable_production_cron=True)
    with pytest.raises(ValueError, match="confirm-production-v1-rollback"):
        render_production_profile("production-v1-rollback", enable_production_cron=True)

