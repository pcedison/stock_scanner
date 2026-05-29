from pathlib import Path

from scripts.check_cloudflare_worker_secrets import missing_secret_names, parse_secret_names
from scripts.check_deployment_preflight import (
    validate_deploy_workflow,
    validate_seed_zip_selection,
    validate_worker_cors,
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


def test_validate_deploy_workflow_accepts_current_guardrails():
    problems = validate_deploy_workflow(Path(".github/workflows/cloudflare-deploy.yml"))

    assert problems == []


def test_cloudflare_worker_secret_parser_detects_missing_bindings():
    output = '[{"name":"SUPER_USER_USERNAME","type":"secret_text"}]'

    assert parse_secret_names(output) == {"SUPER_USER_USERNAME"}
    assert missing_secret_names({"SUPER_USER_USERNAME"}, ["SUPER_USER_USERNAME"]) == []
    assert missing_secret_names(set(), ["SUPER_USER_USERNAME"]) == ["SUPER_USER_USERNAME"]


def test_validate_workflow_yaml_accepts_current_workflows():
    assert validate_workflow_yaml(Path(".github/workflows")) == []


def test_seed_zip_selection_uses_date_resolver_instead_of_mtime():
    assert validate_seed_zip_selection(Path(".github/workflows")) == []


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
      - run: python scripts/check_cloudflare_health.py --url "$CF_WORKER_HEALTH_URL" --max-cache-age-hours 36 --reject-offline-seed
      - run: python scripts/run_remote_smoke.py --health-url "$CF_WORKER_HEALTH_URL" --max-cache-age-hours 36 --reject-offline-seed
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


def test_r2_refresh_job_completion_is_scoped_to_claiming_run():
    text = Path(".github/workflows/cloudflare-r2-seed-refresh.yml").read_text(encoding="utf-8")

    assert (
        "UPDATE refresh_jobs SET status = 'running', owner_run_id = '${GITHUB_RUN_ID}', "
        "started_at = datetime('now'), updated_at = datetime('now') "
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


def test_seed_refresh_workflow_surfaces_manual_pr_when_actions_cannot_create_one():
    text = Path(".github/workflows/refresh-cloudflare-seed.yml").read_text(encoding="utf-8")

    assert "manual_pr_url=" in text
    assert "GitHub Actions is not permitted to create the PR automatically" in text
    assert "Seed rebuild and validation completed before PR creation" in text
