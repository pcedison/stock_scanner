from pathlib import Path

from scripts.check_deployment_preflight import validate_deploy_workflow, validate_worker_cors


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
