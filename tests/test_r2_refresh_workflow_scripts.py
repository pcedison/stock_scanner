from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import scripts.r2_refresh_decision as r2
from scripts.cloudflare_seed_upload_plan import build_upload_plan

JOB_CHECK_ERROR = "D1 pending-job query unavailable"
SECRET_SENTINEL = "secret-D1-response-must-not-leak"


def _health_payload(
    *,
    next_refresh_after: str,
    checked_at: str = "2026-07-12T00:00:00+00:00",
    status: str = "ok",
    is_stale: bool = False,
) -> dict[str, object]:
    return {
        "status": status,
        "cache": {"sourceLastCheckedAt": checked_at},
        "cacheStatus": {
            "isStale": is_stale,
            "nextRefreshAfter": next_refresh_after,
        },
    }


def _cloudflare_pending_payload(count: object) -> dict[str, object]:
    return {
        "success": True,
        "result": [
            {
                "success": True,
                "results": [{"cnt": count}],
            }
        ],
    }


def test_r2_refresh_decision_reads_cloudflare_and_wrangler_count_shapes():
    cloudflare_payload = {"result": [{"results": [{"cnt": 3}]}]}
    wrangler_payload = [{"results": [{"pending_count": 2}]}]

    assert r2.find_count(cloudflare_payload) == 3
    assert r2.find_count(wrangler_payload) == 2


def test_r2_refresh_decision_treats_stale_health_as_refresh_trigger():
    decision = r2.early_refresh_decision(
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
    decision = r2.early_refresh_decision(
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

    (data_dir / "monthly_revenue_history.json").unlink()
    with pytest.raises(FileNotFoundError, match="monthly_revenue_history.json"):
        build_upload_plan(seed_dir, data_dir)


def test_r2_refresh_restores_persisted_monthly_history_before_build():
    workflow = Path(".github/workflows/cloudflare-r2-seed-refresh.yml").read_text(encoding="utf-8")
    get_command = 'r2 object get "$CF_R2_BUCKET/official/monthly_revenue_history.json"'
    hydrate_command = "python scripts/hydrate_cloudflare_seed_inputs.py"
    build_command = "CLOUDFLARE_SEED_MODE=online python scripts/build_cloudflare_seed.py"

    assert get_command in workflow
    assert hydrate_command in workflow
    assert workflow.index(get_command) < workflow.index(hydrate_command) < workflow.index(build_command)
    assert "unzip -o" not in workflow
    assert len(workflow.splitlines()) <= 260


def test_standard_seed_refresh_uses_the_same_validated_hydration_path():
    workflow = Path(".github/workflows/refresh-cloudflare-seed.yml").read_text(encoding="utf-8")
    hydrate_command = "python scripts/hydrate_cloudflare_seed_inputs.py --data-dir data"
    build_command = "CLOUDFLARE_SEED_MODE=online python scripts/build_cloudflare_seed.py"

    assert hydrate_command in workflow
    assert workflow.index(hydrate_command) < workflow.index(build_command)
    assert "unzip -o" not in workflow


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
    on_error = r2.early_refresh_decision(
        force=False, pending_count=0, health_payload=None, health_error="boom",
        health_url_configured=True, max_cache_age_hours=36,
    )
    assert on_error["staleRefresh"] is True and on_error["runRefresh"] is True

    missing_ts = r2.early_refresh_decision(
        force=False, pending_count=0, health_payload={"cache": {}}, health_error="",
        health_url_configured=True, max_cache_age_hours=36,
    )
    assert missing_ts["staleRefresh"] is True

    fresh = r2.early_refresh_decision(
        force=False, pending_count=0,
        health_payload={"cache": {"sourceLastCheckedAt": "2026-05-22T00:00:00+00:00"}},
        health_error="", health_url_configured=True, max_cache_age_hours=36,
        now=datetime(2026, 5, 22, 1, 0, tzinfo=UTC),
    )
    assert fresh["staleRefresh"] is False and fresh["runRefresh"] is False
    assert any("fresh" in message for message in fresh["messages"])

    configured_no_payload = r2.early_refresh_decision(
        force=False, pending_count=0, health_payload=None, health_error="",
        health_url_configured=True, max_cache_age_hours=36,
    )
    assert configured_no_payload["staleRefresh"] is True

    pending_only = r2.early_refresh_decision(
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


def test_refresh_due_at_prefers_top_level_policy_and_falls_back_to_cache():
    top_level = r2.refresh_due_at(
        {
            "cacheStatus": {"nextRefreshAfter": "2026-07-12T02:00:00+00:00"},
            "cache": {"nextRefreshAfter": "2026-07-12T03:00:00+00:00"},
        }
    )
    fallback = r2.refresh_due_at(
        {
            "cacheStatus": {"nextRefreshAfter": "invalid"},
            "cache": {"nextRefreshAfter": "2026-07-12T03:00:00+00:00"},
        }
    )

    assert top_level == datetime(2026, 7, 12, 2, 0, tzinfo=UTC)
    assert fallback == datetime(2026, 7, 12, 3, 0, tzinfo=UTC)
    assert r2.refresh_due_at({}) is None


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime(2026, 7, 12, 0, 59, 59, tzinfo=UTC), False),
        (datetime(2026, 7, 12, 1, 0, 0, tzinfo=UTC), True),
        (datetime(2026, 7, 12, 1, 0, 1, tzinfo=UTC), True),
    ],
    ids=("one-second-before", "exact-boundary", "one-second-after"),
)
def test_early_refresh_decision_uses_proactive_refresh_boundary(now, expected):
    decision = r2.early_refresh_decision(
        force=False,
        pending_count=0,
        health_payload=_health_payload(next_refresh_after="2026-07-12T02:00:00+00:00"),
        health_error="",
        job_check_error="",
        health_url_configured=True,
        max_cache_age_hours=36,
        refresh_ahead_minutes=60,
        now=now,
    )

    assert decision["runRefresh"] is expected
    assert decision["staleRefresh"] is expected


def test_early_refresh_decision_function_default_refreshes_55_minutes_ahead():
    decision = r2.early_refresh_decision(
        force=False,
        pending_count=0,
        health_payload=_health_payload(next_refresh_after="2026-07-12T02:00:00+00:00"),
        health_error="",
        health_url_configured=True,
        max_cache_age_hours=36,
        now=datetime(2026, 7, 12, 1, 5, tzinfo=UTC),
    )

    assert decision["runRefresh"] is True
    assert decision["staleRefresh"] is True


def test_early_refresh_decision_cli_default_refreshes_55_minutes_ahead(tmp_path, capsys):
    current = datetime.now(UTC)
    health = tmp_path / "health.json"
    health.write_text(
        json.dumps(
            _health_payload(
                next_refresh_after=(current + timedelta(minutes=55)).isoformat(),
                checked_at=current.isoformat(),
            )
        ),
        encoding="utf-8",
    )
    output = tmp_path / "output.txt"

    rc = r2.main(
        [
            "early-check",
            "--health-json-file",
            str(health),
            "--health-url-configured",
            "--github-output",
            str(output),
        ]
    )

    assert rc == 0
    decision = json.loads(capsys.readouterr().out)
    assert decision["runRefresh"] is True
    assert decision["staleRefresh"] is True
    assert "run_refresh=true" in output.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("payload", "now"),
    [
        (
            _health_payload(
                next_refresh_after="2026-07-12T12:00:00+00:00",
                is_stale=True,
            ),
            datetime(2026, 7, 12, 1, 0, tzinfo=UTC),
        ),
        (
            _health_payload(
                next_refresh_after="2026-07-12T12:00:00+00:00",
                status="degraded",
            ),
            datetime(2026, 7, 12, 1, 0, tzinfo=UTC),
        ),
        (
            _health_payload(
                next_refresh_after="2026-07-13T12:00:00+00:00",
                checked_at="2026-07-10T12:00:00+00:00",
            ),
            datetime(2026, 7, 12, 1, 0, tzinfo=UTC),
        ),
    ],
    ids=("reported-stale", "non-ok-health", "37-hour-safety-ceiling"),
)
def test_early_refresh_decision_health_signals_override_future_boundary(payload, now):
    decision = r2.early_refresh_decision(
        force=False,
        pending_count=0,
        health_payload=payload,
        health_error="",
        health_url_configured=True,
        max_cache_age_hours=36,
        now=now,
    )

    assert decision["runRefresh"] is True
    assert decision["staleRefresh"] is True


def test_early_refresh_decision_healthy_future_policy_does_not_refresh():
    decision = r2.early_refresh_decision(
        force=False,
        pending_count=0,
        health_payload=_health_payload(next_refresh_after="2026-07-12T12:00:00+00:00"),
        health_error="",
        health_url_configured=True,
        max_cache_age_hours=36,
        now=datetime(2026, 7, 12, 1, 0, tzinfo=UTC),
    )

    assert decision["runRefresh"] is False
    assert decision["staleRefresh"] is False


def test_early_refresh_decision_job_check_error_exact_payload_is_not_stale():
    decision = r2.early_refresh_decision(
        force=False,
        pending_count=0,
        health_payload={"cacheStatus": {"nextRefreshAfter": "2026-07-12T12:00:00+00:00"}},
        health_error="",
        job_check_error=JOB_CHECK_ERROR,
        health_url_configured=True,
        max_cache_age_hours=36,
        now=datetime(2026, 7, 12, 1, 0, tzinfo=UTC),
    )

    assert decision["runRefresh"] is True
    assert decision["staleRefresh"] is False
    assert any("pending-job" in message for message in decision["messages"])


def test_early_refresh_decision_job_check_error_with_healthy_age_is_not_stale():
    decision = r2.early_refresh_decision(
        force=False,
        pending_count=0,
        health_payload=_health_payload(next_refresh_after="2026-07-12T12:00:00+00:00"),
        health_error="",
        job_check_error=JOB_CHECK_ERROR,
        health_url_configured=True,
        max_cache_age_hours=36,
        now=datetime(2026, 7, 12, 1, 0, tzinfo=UTC),
    )

    assert decision["runRefresh"] is True
    assert decision["staleRefresh"] is False


def test_early_refresh_decision_cli_job_error_is_safe_and_not_stale(tmp_path, capsys):
    output = tmp_path / "output.txt"
    summary = tmp_path / "summary.md"

    rc = r2.main(
        [
            "early-check",
            "--job-check-error",
            SECRET_SENTINEL,
            "--github-output",
            str(output),
            "--github-step-summary",
            str(summary),
        ]
    )

    captured = capsys.readouterr()
    assert rc == 0
    decision = json.loads(captured.out)
    assert decision["runRefresh"] is True
    assert decision["staleRefresh"] is False
    assert "run_refresh=true" in output.read_text(encoding="utf-8")
    assert "stale_refresh=false" in output.read_text(encoding="utf-8")
    exposed = "\n".join(
        (
            captured.out,
            captured.err,
            output.read_text(encoding="utf-8"),
            summary.read_text(encoding="utf-8"),
        )
    )
    assert SECRET_SENTINEL not in exposed
    assert JOB_CHECK_ERROR in exposed


def test_cloudflare_pending_count_accepts_exact_success_shape_with_zero():
    assert r2.cloudflare_pending_count(_cloudflare_pending_payload(0)) == 0
    assert r2.cloudflare_pending_count(_cloudflare_pending_payload(3)) == 3


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {"success": False, "errors": [{"message": SECRET_SENTINEL}]},
        {"success": 1, "errors": [{"message": SECRET_SENTINEL}]},
        {"success": "true", "errors": [{"message": SECRET_SENTINEL}]},
        {"success": True, "errors": [{"message": SECRET_SENTINEL}]},
        {"success": True, "result": [], "errors": [{"message": SECRET_SENTINEL}]},
        {
            "success": True,
            "result": [{"success": False, "errors": [{"message": SECRET_SENTINEL}]}],
        },
        {
            "success": True,
            "result": [{"success": "true", "results": [{"cnt": 0}]}],
            "errors": [{"message": SECRET_SENTINEL}],
        },
        {
            "success": True,
            "result": [{"success": True}],
            "errors": [{"message": SECRET_SENTINEL}],
        },
        {
            "success": True,
            "result": [{"success": True, "results": []}],
            "errors": [{"message": SECRET_SENTINEL}],
        },
        {
            "success": True,
            "result": [{"success": True, "results": [{}]}],
            "errors": [{"message": SECRET_SENTINEL}],
        },
        {
            "success": True,
            "result": [{"success": True, "results": [{"cnt": "0"}]}],
            "errors": [{"message": SECRET_SENTINEL}],
        },
        {
            "success": True,
            "result": [{"success": True, "results": [{"cnt": False}]}],
            "errors": [{"message": SECRET_SENTINEL}],
        },
    ],
    ids=(
        "non-object-null",
        "non-object-list",
        "outer-explicit-failure",
        "outer-non-boolean-success",
        "outer-string-success",
        "missing-result",
        "missing-query-result",
        "query-explicit-failure",
        "query-non-boolean-success",
        "missing-results",
        "missing-row",
        "missing-count",
        "string-count",
        "boolean-count",
    ),
)
def test_invalid_cloudflare_response_fails_open_without_exposing_body(payload, tmp_path, capsys):
    api_file = tmp_path / "api.json"
    api_file.write_text(json.dumps(payload), encoding="utf-8")
    output = tmp_path / "output.txt"
    summary = tmp_path / "summary.md"

    rc = r2.main(
        [
            "early-check",
            "--cloudflare-api-json-file",
            str(api_file),
            "--github-output",
            str(output),
            "--github-step-summary",
            str(summary),
        ]
    )

    captured = capsys.readouterr()
    assert rc == 0
    decision = json.loads(captured.out)
    assert decision["runRefresh"] is True
    assert decision["staleRefresh"] is False
    assert decision["pendingCount"] == 0
    exposed = "\n".join(
        (
            json.dumps(decision),
            captured.out,
            captured.err,
            output.read_text(encoding="utf-8"),
            summary.read_text(encoding="utf-8"),
        )
    )
    assert SECRET_SENTINEL not in exposed
    assert JOB_CHECK_ERROR in exposed


def test_malformed_cloudflare_response_fails_open_without_exposing_body(tmp_path, capsys):
    api_file = tmp_path / "api.json"
    api_file.write_text('{"secret": "' + SECRET_SENTINEL, encoding="utf-8")
    output = tmp_path / "output.txt"
    summary = tmp_path / "summary.md"

    rc = r2.main(
        [
            "early-check",
            "--cloudflare-api-json-file",
            str(api_file),
            "--github-output",
            str(output),
            "--github-step-summary",
            str(summary),
        ]
    )

    captured = capsys.readouterr()
    assert rc == 0
    decision = json.loads(captured.out)
    assert decision["runRefresh"] is True
    assert decision["staleRefresh"] is False
    exposed = "\n".join(
        (
            captured.out,
            captured.err,
            output.read_text(encoding="utf-8"),
            summary.read_text(encoding="utf-8"),
        )
    )
    assert SECRET_SENTINEL not in exposed
    assert JOB_CHECK_ERROR in exposed


def test_exact_cloudflare_zero_pending_response_remains_a_successful_skip(tmp_path, capsys):
    api_file = tmp_path / "api.json"
    api_file.write_text(json.dumps(_cloudflare_pending_payload(0)), encoding="utf-8")
    output = tmp_path / "output.txt"

    rc = r2.main(
        [
            "early-check",
            "--cloudflare-api-json-file",
            str(api_file),
            "--github-output",
            str(output),
        ]
    )

    assert rc == 0
    decision = json.loads(capsys.readouterr().out)
    assert decision["pendingCount"] == 0
    assert decision["runRefresh"] is False
    assert decision["staleRefresh"] is False
    assert "run_refresh=false" in output.read_text(encoding="utf-8")
