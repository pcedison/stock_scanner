from pathlib import Path

from scripts.check_operational_readiness import (
    valid_health_url,
    validate_external_environment,
    validate_local_readiness,
)


def _write_readiness_fixture(
    root: Path,
    *,
    deploy_group: str = "cloudflare-production",
    r2_group: str = "cloudflare-production",
    deploy_doc: str | None = None,
) -> None:
    workflow_dir = root / ".github" / "workflows"
    workflow_dir.mkdir(parents=True)
    (root / "docs").mkdir()

    (workflow_dir / "cloudflare-deploy.yml").write_text(
        f"""
name: Deploy

concurrency:
  group: {deploy_group}

jobs:
  deploy:
    environment: production
    steps:
      - uses: actions/upload-artifact@v6
        with:
          name: d1-pre-deploy-backup
      - run: echo "$CF_WORKER_HEALTH_URL"
      - run: python scripts/run_remote_smoke.py
      - run: npx wrangler rollback --yes
""".strip(),
        encoding="utf-8",
    )
    (workflow_dir / "cloudflare-health-monitor.yml").write_text("name: Health\n", encoding="utf-8")
    (workflow_dir / "refresh-cloudflare-seed.yml").write_text("name: Seed\n", encoding="utf-8")
    (workflow_dir / "cloudflare-r2-seed-refresh.yml").write_text(
        f"""
name: R2 Seed

concurrency:
  group: {r2_group}

jobs:
  refresh:
    steps:
      - run: |
          SELECT COUNT(*) AS pending_count FROM refresh_jobs
          unzip -o "$SEED_ZIP" -d data
          CLOUDFLARE_SEED_MODE=online python scripts/build_cloudflare_seed.py
          python scripts/cloudflare_seed_upload_plan.py
          echo "UPDATE refresh_jobs SET status = 'success' WHERE status = 'running' AND owner_run_id = '${{GITHUB_RUN_ID}}'"
          echo "UPDATE refresh_jobs SET status = 'failed' WHERE status = 'running' AND owner_run_id = '${{GITHUB_RUN_ID}}'"
          python scripts/run_remote_smoke.py
""".strip(),
        encoding="utf-8",
    )
    (root / "docs" / "cloudflare_deployment.md").write_text(
        deploy_doc
        or """
CF_WORKER_HEALTH_URL is required.
`.github/workflows/cloudflare-r2-seed-refresh.yml` shares `cloudflare-production`.
Team-owned repositories use production environment reviewers.
Solo-maintainer repositories document the exception and use branch protection with required status checks.
""".strip(),
        encoding="utf-8",
    )


def test_local_operational_readiness_accepts_current_guardrails():
    assert validate_local_readiness() == []


def test_local_operational_readiness_rejects_mismatched_production_concurrency(tmp_path):
    _write_readiness_fixture(
        tmp_path,
        deploy_group="cloudflare-deploy-production",
        r2_group="cloudflare-r2-seed-refresh",
    )

    problems = validate_local_readiness(tmp_path)

    assert any("deploy workflow concurrency group" in problem for problem in problems)
    assert any("R2 seed refresh workflow concurrency group" in problem for problem in problems)
    assert any("cloudflare-production" in problem for problem in problems)


def test_local_operational_readiness_requires_solo_branch_protection_guidance(tmp_path):
    _write_readiness_fixture(
        tmp_path,
        deploy_doc="""
CF_WORKER_HEALTH_URL is required.
`.github/workflows/cloudflare-r2-seed-refresh.yml` shares `cloudflare-production`.
Team-owned repositories use production environment reviewers.
""".strip(),
    )

    problems = validate_local_readiness(tmp_path)

    assert any("solo branch-protection" in problem for problem in problems)


def test_cloudflare_deployment_doc_uses_latest_seed_artifact_pattern():
    text = Path("docs/cloudflare_deployment.md").read_text(encoding="utf-8")

    assert "official_cache_seed_2026-05-14" not in text
    assert "data/official_cache_seed_*.zip" in text


def test_health_url_validation_requires_https_health_path():
    assert valid_health_url("https://example.com/api/health") is True
    assert valid_health_url("http://example.com/api/health") is False
    assert valid_health_url("https://example.com/health") is False


def test_external_environment_validation_reports_missing_values(monkeypatch):
    monkeypatch.delenv("CF_WORKER_HEALTH_URL", raising=False)
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)

    problems = validate_external_environment(require_external=True)

    assert any("CF_WORKER_HEALTH_URL" in problem for problem in problems)
    assert any("CLOUDFLARE_ACCOUNT_ID" in problem for problem in problems)
    assert any("CLOUDFLARE_API_TOKEN" in problem for problem in problems)
