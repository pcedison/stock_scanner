import shlex
from pathlib import Path

import pytest

from scripts.check_operational_readiness import (
    valid_health_url,
    validate_external_environment,
    validate_local_readiness,
)

HEALTH_CHECK_PATHS = (
    "scripts/check_cloudflare_health.py",
    "scripts/run_remote_smoke.py",
)
REQUIRED_HEALTH_OPTIONS = (
    ("--max-refresh-delay-minutes", "15"),
    ("--max-cache-age-hours", "36"),
)


def _workflow_python_commands(workflow_text: str) -> list[list[str]]:
    lines = workflow_text.splitlines()
    commands = []
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        command_start = stripped.removeprefix("if ")
        is_command = not stripped.startswith("#") and command_start.startswith("python ")
        if not is_command or not stripped.endswith("\\"):
            index += 1
            continue
        parts = [stripped]
        while parts[-1].endswith("\\") and index + 1 < len(lines):
            index += 1
            parts.append(lines[index].strip())
        joined = " ".join(part.removesuffix("\\").rstrip() for part in parts)
        tokens = shlex.split(joined, comments=True, posix=True)
        if tokens[:1] == ["if"]:
            tokens = tokens[1:]
        if len(tokens) >= 2 and tokens[0] == "python" and tokens[1] in HEALTH_CHECK_PATHS:
            commands.append(tokens)
        index += 1
    return commands


def _assert_health_command_options(command: list[str]) -> None:
    assert command[:1] == ["python"]
    assert command[1] in HEALTH_CHECK_PATHS
    for option, value in REQUIRED_HEALTH_OPTIONS:
        option_indexes = [index for index, token in enumerate(command) if token == option]
        assert len(option_indexes) == 1
        option_index = option_indexes[0]
        assert command[option_index + 1 : option_index + 2] == [value]


def _write_readiness_fixture(
    root: Path,
    *,
    deploy_group: str = "cloudflare-production",
    r2_group: str = "cloudflare-production",
    deploy_doc: str | None = None,
    health_text: str | None = None,
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
    (workflow_dir / "cloudflare-health-monitor.yml").write_text(
        health_text
        or """
name: Health

jobs:
  health:
    steps:
      - run: |
          set -o pipefail
          python scripts/check_cloudflare_health.py | tee .tmp-health.json
      - run: |
          set -o pipefail
          python scripts/run_remote_smoke.py | tee .tmp-smoke.json
""".strip(),
        encoding="utf-8",
    )
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


def test_health_workflows_configure_refresh_grace_and_cache_age_ceiling():
    workflows = (
        (Path(".github/workflows/cloudflare-health-monitor.yml"), 2),
        (Path(".github/workflows/cloudflare-r2-seed-refresh.yml"), 3),
    )

    for path, expected_count in workflows:
        commands = _workflow_python_commands(path.read_text(encoding="utf-8"))
        assert len(commands) == expected_count
        for command in commands:
            _assert_health_command_options(command)


def test_workflow_command_options_reject_inline_shell_comments():
    workflow_text = "\n".join(
        (
            "python scripts/check_cloudflare_health.py \\",
            "  --max-cache-age-hours 36 # --max-refresh-delay-minutes 15",
        )
    )
    command = _workflow_python_commands(workflow_text)[0]

    with pytest.raises(AssertionError):
        _assert_health_command_options(command)


def test_workflow_command_options_reject_duplicate_options():
    workflow_text = "\n".join(
        (
            "python scripts/run_remote_smoke.py \\",
            "  --max-refresh-delay-minutes 15 \\",
            "  --max-refresh-delay-minutes 15 \\",
            "  --max-cache-age-hours 36",
        )
    )
    command = _workflow_python_commands(workflow_text)[0]

    with pytest.raises(AssertionError):
        _assert_health_command_options(command)


def test_workflow_python_commands_reject_suffix_lookalikes_and_comments():
    workflow_text = "\n".join(
        (
            "# python scripts/check_cloudflare_health.py \\",
            "#   --max-refresh-delay-minutes 15 \\",
            "#   --max-cache-age-hours 36",
            "python scripts/check_cloudflare_health.py.bak \\",
            "  --max-refresh-delay-minutes 15 \\",
            "  --max-cache-age-hours 36",
        )
    )

    assert _workflow_python_commands(workflow_text) == []


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


def test_local_operational_readiness_requires_health_monitor_pipefail(tmp_path):
    _write_readiness_fixture(
        tmp_path,
        health_text="""
name: Health

jobs:
  health:
    steps:
      - run: python scripts/check_cloudflare_health.py | tee .tmp-health.json
      - run: python scripts/run_remote_smoke.py | tee .tmp-smoke.json
""".strip(),
    )

    problems = validate_local_readiness(tmp_path)

    assert any("health monitor" in problem and "pipefail" in problem for problem in problems)


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
