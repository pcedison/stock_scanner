from __future__ import annotations

import argparse
import os
import re
import sys
import tomllib
from pathlib import Path
from urllib.parse import urlparse


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_WRANGLER = ROOT_DIR / "cloudflare" / "wrangler.toml"
DEFAULT_DEPLOY_WORKFLOW = ROOT_DIR / ".github" / "workflows" / "cloudflare-deploy.yml"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _csv(value: str | None) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def _is_local_origin(origin: str) -> bool:
    return (urlparse(origin).hostname or "").lower() in LOCAL_HOSTS


def validate_worker_cors(wrangler_path: Path = DEFAULT_WRANGLER) -> list[str]:
    problems: list[str] = []
    config = tomllib.loads(wrangler_path.read_text(encoding="utf-8"))
    vars_config = config.get("vars") or {}
    app_env = str(vars_config.get("APP_ENV") or "").strip().lower()
    origins = _csv(vars_config.get("APP_CORS_ALLOW_ORIGINS") or vars_config.get("WORKER_CORS_ALLOW_ORIGINS"))

    if app_env not in {"prod", "production"}:
        problems.append("cloudflare/wrangler.toml must set [vars].APP_ENV to production")
    if not origins:
        problems.append("Worker production CORS allowlist is empty")
    for origin in origins:
        parsed = urlparse(origin)
        if parsed.scheme != "https":
            problems.append(f"Worker production CORS origin must use https: {origin}")
        if _is_local_origin(origin):
            problems.append(f"Worker production CORS origin must not be local: {origin}")

    return problems


def validate_deploy_workflow(workflow_path: Path = DEFAULT_DEPLOY_WORKFLOW) -> list[str]:
    problems: list[str] = []
    text = workflow_path.read_text(encoding="utf-8")
    required_patterns = {
        "concurrency": r"(?m)^concurrency:",
        "production environment": r"(?m)^\s+environment:\s+production\s*$",
        "D1 export": r"wrangler d1 export",
        "D1 migrations": r"wrangler d1 migrations apply",
        "Worker dry-run": r"wrangler deploy .*--dry-run",
        "market scan summary R2 upload": r"market_scan_summary\.json",
        "holding analysis R2 upload": r"holding_analysis_shards",
        "post-deploy health": r"scripts/check_cloudflare_health\.py",
        "post-deploy remote smoke": r"scripts/run_remote_smoke\.py",
        "rollback": r"wrangler rollback",
        "non-interactive rollback": r"wrangler rollback .*--yes",
        "health URL variable": r"CF_WORKER_HEALTH_URL",
    }
    for label, pattern in required_patterns.items():
        if not re.search(pattern, text):
            problems.append(f"Deploy workflow is missing {label}")
    return problems


def validate_health_url(require_health_url: bool) -> list[str]:
    if not require_health_url:
        return []
    value = os.getenv("CF_WORKER_HEALTH_URL", "").strip()
    if not value:
        return ["CF_WORKER_HEALTH_URL must be set for production deploy verification"]
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc or not parsed.path.endswith("/api/health"):
        return ["CF_WORKER_HEALTH_URL must be an https URL ending in /api/health"]
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check deployment guardrails before Cloudflare deploys.")
    parser.add_argument("--wrangler", type=Path, default=DEFAULT_WRANGLER)
    parser.add_argument("--deploy-workflow", type=Path, default=DEFAULT_DEPLOY_WORKFLOW)
    parser.add_argument("--require-health-url", action="store_true")
    args = parser.parse_args(argv)

    problems = [
        *validate_worker_cors(args.wrangler),
        *validate_deploy_workflow(args.deploy_workflow),
        *validate_health_url(args.require_health_url),
    ]
    if problems:
        print("Deployment preflight failed:", file=sys.stderr)
        for problem in problems:
            print(f"- {problem}", file=sys.stderr)
        return 1

    print("Deployment preflight passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
