from pathlib import Path

from scripts.check_deployment_preflight import validate_deploy_workflow, validate_worker_cors, validate_workflow_yaml


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


def test_validate_workflow_yaml_accepts_current_workflows():
    assert validate_workflow_yaml(Path(".github/workflows")) == []


def test_validate_workflow_yaml_rejects_invalid_workflow(tmp_path):
    workflow_dir = tmp_path / "workflows"
    workflow_dir.mkdir()
    (workflow_dir / "bad.yml").write_text(
        "name: bad\njobs:\n  test:\n    steps:\n      - run: |\nimport sys\n",
        encoding="utf-8",
    )

    problems = validate_workflow_yaml(workflow_dir)

    assert any("bad.yml" in problem for problem in problems)


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
