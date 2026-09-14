import re
import sqlite3
import tomllib
from pathlib import Path

import pytest
import yaml

from scripts import check_deployment_preflight as deployment_preflight
from scripts.check_cloudflare_worker_secrets import dispatch_enabled, missing_secret_names, parse_secret_names
from scripts.check_deployment_preflight import (
    validate_deploy_workflow,
    validate_seed_zip_selection,
    validate_worker_cors,
    validate_worker_release_defaults,
    validate_workflow_yaml,
)


def test_validate_worker_cors_rejects_local_or_insecure_production_origins(tmp_path):
    wrangler = tmp_path / "wrangler.toml"
    wrangler.write_text(
        """
name = "demo"

[vars]
APP_ENV = "production"
APP_CORS_ALLOW_ORIGINS = "https://app.example,http://localhost:8787,http://bad.example"
""".strip(),
        encoding="utf-8",
    )

    problems = validate_worker_cors(wrangler)

    assert any("local" in problem for problem in problems)
    assert any("https" in problem for problem in problems)


def test_worker_observability_requires_persisted_logs_and_sampled_traces(tmp_path):
    wrangler = tmp_path / "wrangler.toml"
    wrangler.write_text('name = "demo"\nmain = "worker.py"\n', encoding="utf-8")

    problems = deployment_preflight.validate_worker_observability(wrangler)

    assert any("observability" in problem for problem in problems)

    wrangler.write_text(
        "[observability]\n"
        "enabled = true\n"
        "[observability.logs]\n"
        "enabled = true\n"
        "head_sampling_rate = 1\n"
        "invocation_logs = true\n"
        "persist = true\n"
        "[observability.traces]\n"
        "enabled = true\n"
        "head_sampling_rate = 0.1\n"
        "persist = true\n",
        encoding="utf-8",
    )

    assert deployment_preflight.validate_worker_observability(wrangler) == []


@pytest.mark.parametrize(
    ("config_text", "expected_problem"),
    [
        ('observability = "enabled"\n', "observability must be a table"),
        ("observability = []\n", "observability must be a table"),
        ('[observability]\nenabled = true\nlogs = "enabled"\n', "observability.logs must be a table"),
        ("[observability]\nenabled = true\nlogs = []\n", "observability.logs must be a table"),
        ('[observability]\nenabled = true\ntraces = "enabled"\n', "observability.traces must be a table"),
        ("[observability]\nenabled = true\ntraces = []\n", "observability.traces must be a table"),
    ],
)
def test_worker_observability_rejects_non_table_sections(tmp_path, config_text, expected_problem):
    wrangler = tmp_path / "wrangler.toml"
    wrangler.write_text(config_text, encoding="utf-8")

    problems = deployment_preflight.validate_worker_observability(wrangler)

    assert expected_problem in problems


def _workflow_schedule_crons(path: Path) -> list[str]:
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    triggers = workflow.get("on", workflow.get(True))
    return [item["cron"] for item in triggers["schedule"]]


def _workflow_step_script(path: Path, step_id: str) -> str:
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            if step.get("id") == step_id:
                return step["run"]
    raise AssertionError(f"workflow step {step_id!r} was not found")


def test_validate_deploy_workflow_accepts_current_guardrails():
    problems = validate_deploy_workflow(Path(".github/workflows/cloudflare-deploy.yml"))

    assert problems == []


def test_deploy_applies_d1_migrations_before_live_worker_upload():
    text = Path(".github/workflows/cloudflare-deploy.yml").read_text(encoding="utf-8")

    migration_index = text.index('wrangler d1 migrations apply "$CF_D1_DATABASE" --remote')
    live_deploy_index = text.index("wrangler deploy --config cloudflare/wrangler.toml --strict")

    assert migration_index < live_deploy_index


def test_cloudflare_worker_secret_parser_detects_missing_bindings():
    output = '[{"name":"SUPER_USER_USERNAME","type":"secret_text"}]'

    assert parse_secret_names(output) == {"SUPER_USER_USERNAME"}
    assert missing_secret_names({"SUPER_USER_USERNAME"}, ["SUPER_USER_USERNAME"]) == []
    assert missing_secret_names(set(), ["SUPER_USER_USERNAME"]) == ["SUPER_USER_USERNAME"]


def test_dispatch_secret_requirement_follows_wrangler_flag(tmp_path):
    wrangler = tmp_path / "wrangler.toml"
    wrangler.write_text('[vars]\nGITHUB_DISPATCH_ENABLED = "false"\n', encoding="utf-8")
    assert dispatch_enabled(wrangler) is False
    assert missing_secret_names({"SUPER_USER_USERNAME"}, ["SUPER_USER_USERNAME"]) == []

    wrangler.write_text('[vars]\nGITHUB_DISPATCH_ENABLED = "true"\n', encoding="utf-8")
    assert dispatch_enabled(wrangler) is True
    required = ["SUPER_USER_USERNAME", "GITHUB_APP_PRIVATE_KEY"]
    assert missing_secret_names({"SUPER_USER_USERNAME"}, required) == ["GITHUB_APP_PRIVATE_KEY"]


def test_production_wrangler_runs_the_refresh_schedule_from_worker_cron():
    config = tomllib.loads(Path("cloudflare/wrangler.toml").read_text(encoding="utf-8"))

    # GitHub `schedule` events were delayed/dropped for hours, so the Worker cron is the
    # only scheduled trigger and must ship with GitHub dispatch enabled.
    assert config["vars"]["GITHUB_DISPATCH_ENABLED"] == "true"
    assert config["vars"]["MARKET_SCAN_API_VERSION"] == "v2"
    assert config["vars"]["EDGE_CACHE_ENABLED"] == "true"
    assert config["cache"]["enabled"] is False
    # Cloudflare cron day-of-week is 1=Sunday (not 0), so weekdays are spelled by name.
    # The morning slot (06:30 Taipei) on Monday is still Sunday in UTC, hence SUN-THU.
    assert config["triggers"]["crons"] == ["*/20 21-23 * * SUN-THU", "*/20 0-15 * * MON-FRI"]
    assert validate_worker_release_defaults(Path("cloudflare/wrangler.toml")) == []
    assert "cloudflare/wrangler.*.generated.toml" in Path(".gitignore").read_text(encoding="utf-8")


def test_validate_worker_release_defaults_rejects_committed_release_switches(tmp_path):
    wrangler = tmp_path / "wrangler.toml"
    wrangler.write_text(
        """
[vars]
GITHUB_DISPATCH_ENABLED = "false"
MARKET_SCAN_API_VERSION = "v1"
EDGE_CACHE_ENABLED = "false"

[cache]
enabled = true

[triggers]
crons = []
""".strip(),
        encoding="utf-8",
    )

    problems = validate_worker_release_defaults(wrangler)

    assert any("GITHUB_DISPATCH_ENABLED" in problem for problem in problems)
    assert any("MARKET_SCAN_API_VERSION" in problem for problem in problems)
    assert any("EDGE_CACHE_ENABLED" in problem for problem in problems)
    assert any("[cache].enabled" in problem for problem in problems)
    assert any("crons" in problem for problem in problems)


def test_validate_workflow_yaml_accepts_current_workflows():
    assert validate_workflow_yaml(Path(".github/workflows")) == []


def test_seed_zip_selection_uses_date_resolver_instead_of_mtime():
    assert validate_seed_zip_selection(Path(".github/workflows")) == []


def test_ci_validates_committed_seed_checksum_sidecar():
    text = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert 'SEED_SHA="${SEED_ZIP%.zip}.sha256"' in text
    assert 'sha256sum --check "$(basename "$SEED_SHA")"' in text


def test_validate_workflow_yaml_rejects_invalid_workflow(tmp_path):
    workflow_dir = tmp_path / "workflows"
    workflow_dir.mkdir()
    (workflow_dir / "bad.yml").write_text(
        "name: bad\njobs:\n  test:\n    steps:\n      - run: |\nimport sys\n",
        encoding="utf-8",
    )

    problems = validate_workflow_yaml(workflow_dir)

    assert any("bad.yml" in problem for problem in problems)


def test_seed_zip_selection_rejects_mtime_based_ls(tmp_path):
    workflow_dir = tmp_path / "workflows"
    workflow_dir.mkdir()
    (workflow_dir / "bad.yml").write_text(
        "name: bad\njobs:\n  test:\n    steps:\n"
        "      - run: SEED_ZIP=$(ls -t data/official_cache_seed_*.zip 2>/dev/null | head -1)\n",
        encoding="utf-8",
    )

    problems = validate_seed_zip_selection(workflow_dir)

    assert any("mtime-based ls -t" in problem for problem in problems)


def test_validate_deploy_workflow_rejects_r2_seed_writes(tmp_path):
    workflow = tmp_path / "deploy.yml"
    workflow.write_text(
        """
concurrency:
  group: demo

jobs:
  deploy:
    environment: production
    steps:
      - run: npx wrangler deploy --config cloudflare/wrangler.toml --dry-run
      - run: npx wrangler r2 object put "$CF_R2_BUCKET/public/manifest.json"
      - run: CLOUDFLARE_SEED_MODE=offline python scripts/build_cloudflare_seed.py
      - run: npx wrangler d1 export demo --remote
      - run: npx wrangler d1 migrations apply demo --remote
      - name: Verify production data safety before mutations
        run: python scripts/check_cloudflare_health.py --url "$CF_WORKER_HEALTH_URL" --max-cache-age-hours 36 --max-refresh-delay-minutes 1440 --reject-offline-seed
      - run: python scripts/run_remote_smoke.py --health-url "$CF_WORKER_HEALTH_URL" --max-cache-age-hours 36 --max-refresh-delay-minutes 1440 --reject-offline-seed
      - run: npx wrangler d1 execute demo --command "UPDATE refresh_jobs SET status = 'success'"
      - run: npx wrangler rollback --yes
""".strip(),
        encoding="utf-8",
    )

    problems = validate_deploy_workflow(workflow)

    assert any("R2 seed upload" in problem for problem in problems)
    assert any("offline seed rebuild" in problem for problem in problems)
    assert any("refresh job success mutation" in problem for problem in problems)


def test_r2_refresh_workflow_can_self_heal_stale_production_seed():
    deploy_text = Path(".github/workflows/cloudflare-deploy.yml").read_text(encoding="utf-8")
    text = Path(".github/workflows/cloudflare-r2-seed-refresh.yml").read_text(encoding="utf-8")

    assert "group: cloudflare-production" in deploy_text
    assert "group: cloudflare-production" in text
    assert "group: cloudflare-r2-seed-refresh" not in text
    assert "--max-cache-age-hours 36" in text
    assert "--reject-offline-seed" in text
    assert "stale_refresh=true" in text
    assert "Production seed freshness check failed; R2 seed rebuild will run." in text


def test_deploy_uses_hard_data_age_gate_without_competing_with_refresh_sla():
    text = Path(".github/workflows/cloudflare-deploy.yml").read_text(encoding="utf-8")

    safety_gate = text.index("- name: Verify production data safety before mutations")
    d1_export = text.index("- name: Export D1 backup before migrations")

    assert safety_gate < d1_export
    assert text.count("--max-cache-age-hours 36") == 3
    assert text.count("--max-refresh-delay-minutes 1440") == 3
    assert text.count("--reject-offline-seed") == 3


def test_active_cloudflare_schedules_and_refresh_options_are_policy_aligned():
    r2_path = Path(".github/workflows/cloudflare-r2-seed-refresh.yml")
    health_path = Path(".github/workflows/cloudflare-health-monitor.yml")
    r2_workflow = r2_path.read_text(encoding="utf-8")
    health_workflow = health_path.read_text(encoding="utf-8")

    # The Worker cron is the primary trigger, but GitHub `schedule` is kept as a
    # backstop: after the 2026-09-11 cutover the Worker cron did not fire and, with
    # `schedule` removed, nothing refreshed the seed for five hours. Both may run - the
    # Worker dispatches force=false so the second arrival is a no-op here.
    r2_triggers = yaml.safe_load(r2_workflow)
    r2_on = r2_triggers.get("on", r2_triggers.get(True))
    # Backstop ticks land just after the 06:30 / 18:00 Taipei publication slots only.
    assert _workflow_schedule_crons(r2_path) == ["7 23 * * 0-4", "7 10,12 * * 1-5"]
    assert "workflow_dispatch" in r2_on
    # The monitor still polls from GitHub every 4 hours and now also fails fast on a dead
    # dispatch token, before the seed itself goes stale.
    assert _workflow_schedule_crons(health_path) == ["11 */4 * * *"]
    # Never rebuild before the publication slot: the datasets have not changed yet.
    assert "--refresh-ahead-minutes 0" in r2_workflow
    assert "--job-check-error" in r2_workflow
    assert "--max-refresh-delay-minutes 75" in health_workflow
    assert "--require-dispatch-healthy" in health_workflow
    assert "steps.early-check.outputs.stale_refresh" in r2_workflow


def test_lightweight_d1_request_fails_open_with_only_a_fixed_error_marker():
    script = _workflow_step_script(
        Path(".github/workflows/cloudflare-r2-seed-refresh.yml"),
        "early-check",
    )

    assert "--output .tmp/early-d1.json" in script
    assert "api_status=$?" in script
    assert 'if [ "$api_status" -eq 0 ]; then' in script
    assert 'job_check_error="D1 pending-job query unavailable"' in script
    assert 'job_check_args=(--job-check-error "$job_check_error")' in script
    assert '"${job_check_args[@]}"' in script
    assert "cat .tmp/early-d1.json" not in script


def test_r2_refresh_job_completion_is_scoped_to_claiming_run():
    text = Path(".github/workflows/cloudflare-r2-seed-refresh.yml").read_text(encoding="utf-8")

    assert (
        "UPDATE refresh_jobs SET status = 'running', dispatch_status = 'workflow_claimed', "
        "owner_run_id = '${GITHUB_RUN_ID}', "
        "dispatch_error_code = NULL, started_at = datetime('now'), updated_at = datetime('now') "
        "WHERE job_type = 'market_scan' AND status = 'queued';"
    ) in text
    assert (
        "UPDATE refresh_jobs SET status = 'success', finished_at = datetime('now'), "
        "updated_at = datetime('now'), error = NULL "
        "WHERE job_type = 'market_scan' AND status = 'running' "
        "AND owner_run_id = '${GITHUB_RUN_ID}';"
    ) in text
    assert (
        "UPDATE refresh_jobs SET status = 'failed', finished_at = datetime('now'), "
        "updated_at = datetime('now'), error = 'GitHub Actions run ${GITHUB_RUN_ID} failed' "
        "WHERE job_type = 'market_scan' AND status = 'running' "
        "AND owner_run_id = '${GITHUB_RUN_ID}';"
    ) in text
    assert "status IN ('queued', 'running')" in text
    assert "SET status = 'success'" in text
    assert "SET status = 'failed'" in text


def test_r2_refresh_workflow_sql_claims_and_finalizes_only_its_jobs():
    text = Path(".github/workflows/cloudflare-r2-seed-refresh.yml").read_text(encoding="utf-8")
    updates = re.findall(r'--command "(UPDATE refresh_jobs SET [^"]+;)"', text)
    claim = next(sql for sql in updates if "dispatch_status = 'workflow_claimed'" in sql)
    success = next(sql for sql in updates if "SET status = 'success'" in sql)
    failed = next(sql for sql in updates if "SET status = 'failed'" in sql)

    database = sqlite3.connect(":memory:")
    database.executescript(Path("cloudflare/schema.sql").read_text(encoding="utf-8"))
    rows = (
        ("pending", "market_scan", "cache-pending", "queued", "pending", "OLD_ERROR"),
        ("dispatched", "market_scan", "cache-dispatched", "queued", "dispatched", "OLD_ERROR"),
        ("dispatch-failed", "market_scan", "cache-failed", "queued", "failed", "GITHUB_HTTP_503"),
        ("dispatch-unknown", "market_scan", "cache-unknown", "queued", "unknown", "UNKNOWN"),
        ("other-type", "other_job", "cache-other", "queued", "pending", "OTHER_ERROR"),
    )
    database.executemany(
        """
        INSERT INTO refresh_jobs
        (id, job_type, cache_key, status, reason, queued_at, updated_at, dispatch_status, dispatch_error_code)
        VALUES (?, ?, ?, ?, 'manual', '2026-07-13', '2026-07-13', ?, ?)
        """,
        rows,
    )

    database.execute(claim.replace("${GITHUB_RUN_ID}", "1001"))
    claimed = dict(database.execute("SELECT id, owner_run_id FROM refresh_jobs"))
    assert claimed == {
        "pending": "1001",
        "dispatched": "1001",
        "dispatch-failed": "1001",
        "dispatch-unknown": "1001",
        "other-type": None,
    }
    assert set(
        database.execute(
            "SELECT status, dispatch_status FROM refresh_jobs WHERE job_type = 'market_scan'"
        )
    ) == {("running", "workflow_claimed")}
    assert set(
        database.execute("SELECT dispatch_error_code FROM refresh_jobs WHERE job_type = 'market_scan'")
    ) == {(None,)}
    assert database.execute("SELECT dispatch_error_code FROM refresh_jobs WHERE id = 'other-type'").fetchone()[0] == "OTHER_ERROR"

    database.execute(
        """
        INSERT INTO refresh_jobs
        (id, job_type, cache_key, status, reason, queued_at, updated_at)
        VALUES ('later', 'market_scan', 'cache-later', 'queued', 'manual', '2026-07-13', '2026-07-13')
        """
    )
    database.execute(claim.replace("${GITHUB_RUN_ID}", "1002"))
    assert database.execute("SELECT owner_run_id FROM refresh_jobs WHERE id = 'pending'").fetchone()[0] == "1001"
    assert database.execute("SELECT owner_run_id FROM refresh_jobs WHERE id = 'later'").fetchone()[0] == "1002"

    database.execute(success.replace("${GITHUB_RUN_ID}", "1001"))
    assert database.execute("SELECT status FROM refresh_jobs WHERE id = 'pending'").fetchone()[0] == "success"
    assert database.execute("SELECT status FROM refresh_jobs WHERE id = 'later'").fetchone()[0] == "running"
    database.execute(failed.replace("${GITHUB_RUN_ID}", "1002"))
    assert database.execute("SELECT status FROM refresh_jobs WHERE id = 'later'").fetchone()[0] == "failed"
    assert database.execute("SELECT status FROM refresh_jobs WHERE id = 'other-type'").fetchone()[0] == "queued"


def test_seed_refresh_workflow_surfaces_manual_pr_when_actions_cannot_create_one():
    text = Path(".github/workflows/refresh-cloudflare-seed.yml").read_text(encoding="utf-8")

    assert "manual_pr_url=" in text
    assert "GitHub Actions is not permitted to create the PR automatically" in text
    assert "Seed rebuild and validation completed before PR creation" in text


def test_nightly_date_sweep_covers_q2_freshness_boundaries():
    text = Path(".github/workflows/nightly-date-sweep.yml").read_text(encoding="utf-8")

    assert "2026-07-01" in text
    assert "2026-08-31" in text
    assert "2026-09-01" in text
