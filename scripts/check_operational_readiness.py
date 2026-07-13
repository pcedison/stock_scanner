from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT_DIR = Path(__file__).resolve().parents[1]
DEPLOY_WORKFLOW = ROOT_DIR / ".github" / "workflows" / "cloudflare-deploy.yml"
HEALTH_WORKFLOW = ROOT_DIR / ".github" / "workflows" / "cloudflare-health-monitor.yml"
SEED_WORKFLOW = ROOT_DIR / ".github" / "workflows" / "refresh-cloudflare-seed.yml"
R2_SEED_REFRESH_WORKFLOW = ROOT_DIR / ".github" / "workflows" / "cloudflare-r2-seed-refresh.yml"
DEPLOY_DOC = ROOT_DIR / "docs" / "cloudflare_deployment.md"
PRODUCTION_CONCURRENCY_GROUP = "cloudflare-production"


def valid_health_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme == "https" and bool(parsed.netloc) and parsed.path.endswith("/api/health")


def _workflow_concurrency_group(text: str) -> str | None:
    for line_number, line in enumerate(text.splitlines()):
        inline = re.match(r"^concurrency:\s+([^#\n]+?)\s*$", line)
        if inline:
            return inline.group(1).strip().strip("'\"")
        if not re.match(r"^concurrency:\s*$", line):
            continue

        for child in text.splitlines()[line_number + 1 :]:
            if child.strip() == "":
                continue
            if not child.startswith((" ", "\t")):
                return None
            group = re.match(r"^\s+group:\s*([^#\n]+?)\s*$", child)
            if group:
                return group.group(1).strip().strip("'\"")
    return None


def validate_local_readiness(root: Path = ROOT_DIR) -> list[str]:
    problems: list[str] = []
    deploy_text = (root / DEPLOY_WORKFLOW.relative_to(ROOT_DIR)).read_text(encoding="utf-8")
    health_path = root / HEALTH_WORKFLOW.relative_to(ROOT_DIR)
    health_exists = health_path.exists()
    health_text = health_path.read_text(encoding="utf-8") if health_exists else ""
    seed_exists = (root / SEED_WORKFLOW.relative_to(ROOT_DIR)).exists()
    r2_seed_refresh_path = root / R2_SEED_REFRESH_WORKFLOW.relative_to(ROOT_DIR)
    r2_seed_refresh_exists = r2_seed_refresh_path.exists()
    r2_seed_refresh_text = r2_seed_refresh_path.read_text(encoding="utf-8") if r2_seed_refresh_exists else ""
    deploy_doc = (root / DEPLOY_DOC.relative_to(ROOT_DIR)).read_text(encoding="utf-8")

    required_patterns = {
        "production environment gate": r"(?m)^\s+environment:\s+production\s*$",
        "production concurrency": r"(?m)^concurrency:",
        "health URL variable": r"CF_WORKER_HEALTH_URL",
        "post-deploy remote smoke": r"scripts/run_remote_smoke\.py",
        "non-interactive Worker rollback": r"wrangler rollback .*--yes",
        "D1 backup artifact": r"d1-pre-deploy-backup",
    }
    for label, pattern in required_patterns.items():
        if not re.search(pattern, deploy_text):
            problems.append(f"deploy workflow missing {label}")
    if _workflow_concurrency_group(deploy_text) != PRODUCTION_CONCURRENCY_GROUP:
        problems.append(f"deploy workflow concurrency group must be shared production group {PRODUCTION_CONCURRENCY_GROUP}")
    if not health_exists:
        problems.append("scheduled health monitor workflow is missing")
    else:
        pipefail_count = len(re.findall(r"set -o pipefail", health_text))
        if pipefail_count < 2:
            problems.append("health monitor must set pipefail for both piped health and smoke checks")
        health_required_patterns = {
            "deployed health check": r"scripts/check_cloudflare_health\.py[\s\S]*?\|\s*tee",
            "remote smoke": r"scripts/run_remote_smoke\.py[\s\S]*?\|\s*tee",
        }
        for label, pattern in health_required_patterns.items():
            if not re.search(pattern, health_text):
                problems.append(f"health monitor workflow missing {label}")
    if not seed_exists:
        problems.append("scheduled seed refresh workflow is missing")
    if not r2_seed_refresh_exists:
        problems.append("scheduled R2 seed refresh workflow is missing")
    else:
        if _workflow_concurrency_group(r2_seed_refresh_text) != PRODUCTION_CONCURRENCY_GROUP:
            problems.append(f"R2 seed refresh workflow concurrency group must be shared production group {PRODUCTION_CONCURRENCY_GROUP}")
        r2_required_patterns = {
            "D1 refresh job polling": r"SELECT COUNT\(\*\) AS pending_count FROM refresh_jobs",
            "persistent seed hydration": r"scripts/hydrate_cloudflare_seed_inputs\.py",
            "online seed rebuild": r"CLOUDFLARE_SEED_MODE=online python scripts/build_cloudflare_seed\.py",
            "testable R2 upload plan": r"scripts/cloudflare_seed_upload_plan\.py",
            "refresh job workflow claim": (
                r"UPDATE refresh_jobs SET status = 'running', dispatch_status = 'workflow_claimed', "
                r"owner_run_id = '\$\{GITHUB_RUN_ID\}'[^\r\n]*WHERE job_type = 'market_scan' AND status = 'queued'"
            ),
            "owner-scoped refresh job success marker": (
                r"UPDATE refresh_jobs SET status = 'success'[^\r\n]*WHERE job_type = 'market_scan' "
                r"AND status = 'running' AND owner_run_id = '\$\{GITHUB_RUN_ID\}'"
            ),
            "owner-scoped refresh job failure marker": (
                r"UPDATE refresh_jobs SET status = 'failed'[^\r\n]*WHERE job_type = 'market_scan' "
                r"AND status = 'running' AND owner_run_id = '\$\{GITHUB_RUN_ID\}'"
            ),
            "remote smoke": r"scripts/run_remote_smoke\.py",
        }
        for label, pattern in r2_required_patterns.items():
            if not re.search(pattern, r2_seed_refresh_text):
                problems.append(f"R2 seed refresh workflow missing {label}")
    deploy_doc_lower = deploy_doc.lower()
    if "CF_WORKER_HEALTH_URL" not in deploy_doc or "cloudflare-r2-seed-refresh.yml" not in deploy_doc:
        problems.append("Cloudflare deployment doc must mention CF_WORKER_HEALTH_URL and the R2 seed refresh workflow")
    if PRODUCTION_CONCURRENCY_GROUP not in deploy_doc:
        problems.append(f"Cloudflare deployment doc must mention the shared {PRODUCTION_CONCURRENCY_GROUP} concurrency group")
    if "team" not in deploy_doc_lower or "environment reviewers" not in deploy_doc_lower:
        problems.append("Cloudflare deployment doc must describe team environment reviewer protection")
    if "solo" not in deploy_doc_lower or "branch protection" not in deploy_doc_lower or "required status checks" not in deploy_doc_lower:
        problems.append("Cloudflare deployment doc must describe the solo branch-protection and required-status-check alternative")
    return problems


def validate_external_environment(require_external: bool) -> list[str]:
    if not require_external:
        return []
    problems: list[str] = []
    health_url = os.getenv("CF_WORKER_HEALTH_URL", "").strip()
    if not valid_health_url(health_url):
        problems.append("CF_WORKER_HEALTH_URL must be an https URL ending in /api/health")
    if not os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip():
        problems.append("CLOUDFLARE_ACCOUNT_ID is not set")
    if not os.getenv("CLOUDFLARE_API_TOKEN", "").strip():
        problems.append("CLOUDFLARE_API_TOKEN is not set")
    return problems


def inspect_github_environment(repo: str, require_live: bool) -> list[str]:
    if not require_live:
        return []
    if not repo:
        return ["--github-repo is required with --require-github-live"]
    if not shutil.which("gh"):
        return ["gh CLI is required for live GitHub environment inspection"]

    problems: list[str] = []
    variable = subprocess.run(
        ["gh", "api", f"repos/{repo}/actions/variables/CF_WORKER_HEALTH_URL"],
        text=True,
        capture_output=True,
        check=False,
    )
    if variable.returncode != 0:
        problems.append("GitHub repository variable CF_WORKER_HEALTH_URL is missing or inaccessible")
    else:
        payload = json.loads(variable.stdout or "{}")
        if not valid_health_url(str(payload.get("value") or "")):
            problems.append("GitHub repository variable CF_WORKER_HEALTH_URL is not a valid /api/health URL")

    environment = subprocess.run(
        ["gh", "api", f"repos/{repo}/environments/production"],
        text=True,
        capture_output=True,
        check=False,
    )
    if environment.returncode != 0:
        problems.append("GitHub production environment is missing or inaccessible")
    else:
        payload = json.loads(environment.stdout or "{}")
        protection_rules = payload.get("protection_rules") or []
        has_reviewers = any(rule.get("type") == "required_reviewers" for rule in protection_rules)
        if not has_reviewers:
            problems.append(
                "GitHub production environment has no required reviewers; team-owned repos should require environment "
                "reviewers, while solo-maintainer repos should document the exception and rely on branch protection "
                "with required status checks"
            )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check operational readiness guardrails for Cloudflare production.")
    parser.add_argument("--require-external", action="store_true", help="Require local Cloudflare/GitHub env vars.")
    parser.add_argument("--github-repo", help="owner/repo for live GitHub environment inspection.")
    parser.add_argument("--require-github-live", action="store_true")
    args = parser.parse_args(argv)

    problems = [
        *validate_local_readiness(),
        *validate_external_environment(args.require_external),
        *inspect_github_environment(args.github_repo or "", args.require_github_live),
    ]
    if problems:
        print("Operational readiness check failed:", file=sys.stderr)
        for problem in problems:
            print(f"- {problem}", file=sys.stderr)
        return 1

    print("Operational readiness check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
