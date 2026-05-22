from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence


ROOT_DIR = Path(__file__).resolve().parents[1]
BLOCKING_RUN_STATUSES = {"pending", "waiting", "in_progress", "queued"}
SUCCESS = "success"


class ToolError(RuntimeError):
    pass


def _run(args: Sequence[str], *, cwd: Path = ROOT_DIR, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            list(args),
            cwd=cwd,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ToolError(f"Required command is not available: {args[0]}") from exc

    if check and completed.returncode != 0:
        stderr = completed.stderr.strip()
        stdout = completed.stdout.strip()
        detail = stderr or stdout or f"exit code {completed.returncode}"
        raise ToolError(f"{' '.join(args)} failed: {detail}")
    return completed


def _git(args: Sequence[str]) -> str:
    return _run(["git", *args]).stdout.strip()


def _gh_json(repo: str, path: str) -> dict[str, Any]:
    completed = _run(["gh", "api", f"/repos/{repo}{path}"])
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise ToolError(f"gh api returned invalid JSON for {path}") from exc
    if not isinstance(payload, dict):
        raise ToolError(f"gh api returned non-object JSON for {path}")
    return payload


def _gh_repo() -> str:
    repo = _run(["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"]).stdout.strip()
    if not repo or "/" not in repo:
        raise ToolError("Could not determine GitHub repository with gh repo view")
    return repo


def collect_git_state() -> dict[str, Any]:
    local_head = _git(["rev-parse", "HEAD"])
    origin_main = _git(["rev-parse", "origin/main"])
    branch = _git(["branch", "--show-current"])
    status = _run(["git", "status", "--porcelain"]).stdout.rstrip("\r\n")
    left_right = _git(["rev-list", "--left-right", "--count", "origin/main...HEAD"]).split()
    behind = int(left_right[0]) if len(left_right) == 2 else None
    ahead = int(left_right[1]) if len(left_right) == 2 else None
    return {
        "branch": branch,
        "localHead": local_head,
        "originMain": origin_main,
        "headMatchesOriginMain": local_head == origin_main,
        "workingTreeClean": status == "",
        "workingTreeChanges": status.splitlines(),
        "aheadOfOriginMain": ahead,
        "behindOriginMain": behind,
    }


def _workflow_runs(repo: str, workflow: str, *, branch: str = "main", per_page: int = 20) -> list[dict[str, Any]]:
    payload = _gh_json(repo, f"/actions/workflows/{workflow}/runs?branch={branch}&per_page={per_page}")
    runs = payload.get("workflow_runs")
    if not isinstance(runs, list):
        raise ToolError(f"Workflow run list is missing for {workflow}")
    return [run for run in runs if isinstance(run, dict)]


def _summarize_run(run: dict[str, Any] | None) -> dict[str, Any] | None:
    if not run:
        return None
    return {
        "id": run.get("id"),
        "name": run.get("name"),
        "workflowId": run.get("workflow_id"),
        "headBranch": run.get("head_branch"),
        "headSha": run.get("head_sha"),
        "status": run.get("status"),
        "conclusion": run.get("conclusion"),
        "event": run.get("event"),
        "htmlUrl": run.get("html_url"),
        "createdAt": run.get("created_at"),
        "updatedAt": run.get("updated_at"),
    }


def latest_workflow_run(repo: str, workflow: str) -> dict[str, Any] | None:
    runs = _workflow_runs(repo, workflow, per_page=10)
    return _summarize_run(runs[0] if runs else None)


def find_stale_deploy_runs(repo: str, workflow: str, origin_main: str) -> list[dict[str, Any]]:
    stale: list[dict[str, Any]] = []
    for run in _workflow_runs(repo, workflow, branch="main", per_page=50):
        status = str(run.get("status") or "").lower()
        head_branch = run.get("head_branch")
        head_sha = run.get("head_sha")
        if head_branch == "main" and head_sha != origin_main and status in BLOCKING_RUN_STATUSES:
            summarized = _summarize_run(run)
            if summarized:
                stale.append(summarized)
    return stale


def cancel_stale_runs(repo: str, stale_runs: list[dict[str, Any]], *, dry_run: bool) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for run in stale_runs:
        run_id = run.get("id")
        result = {"id": run_id, "headSha": run.get("headSha"), "dryRun": dry_run, "cancelled": False}
        if not dry_run:
            _run(["gh", "api", "--method", "POST", f"/repos/{repo}/actions/runs/{run_id}/cancel"])
            result["cancelled"] = True
        results.append(result)
    return results


def evaluate_run(run: dict[str, Any] | None, origin_main: str, label: str) -> list[str]:
    if run is None:
        return [f"No {label} workflow run found on main"]
    problems: list[str] = []
    if run.get("headSha") != origin_main:
        problems.append(f"Latest {label} run is for {run.get('headSha')}, expected origin/main {origin_main}")
    if run.get("status") != "completed" or run.get("conclusion") != SUCCESS:
        problems.append(
            f"Latest {label} run is {run.get('status')}/{run.get('conclusion')}, expected completed/success"
        )
    return problems


def run_optional_check(command: list[str]) -> dict[str, Any]:
    completed = _run(command, check=False)
    return {
        "command": command,
        "ok": completed.returncode == 0,
        "returnCode": completed.returncode,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
    }


def production_health_command(args: argparse.Namespace) -> list[str]:
    command = [
        sys.executable,
        str(ROOT_DIR / "scripts" / "check_cloudflare_health.py"),
        "--url",
        args.health_url,
    ]
    if args.manifest:
        command.extend(["--manifest", str(args.manifest)])
    if args.max_cache_age_hours is not None:
        command.extend(["--max-cache-age-hours", str(args.max_cache_age_hours)])
    if not args.allow_offline_seed:
        command.append("--reject-offline-seed")
    return command


def production_smoke_command(args: argparse.Namespace) -> list[str]:
    command = [
        sys.executable,
        str(ROOT_DIR / "scripts" / "run_remote_smoke.py"),
        "--health-url",
        args.health_url,
    ]
    if args.manifest:
        command.extend(["--manifest", str(args.manifest)])
    if args.max_cache_age_hours is not None:
        command.extend(["--max-cache-age-hours", str(args.max_cache_age_hours)])
    if not args.allow_offline_seed:
        command.append("--reject-offline-seed")
    return command


def build_summary(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    repo = args.repo or _gh_repo()
    git_state = collect_git_state()
    validate_run = latest_workflow_run(repo, args.validate_workflow)
    deploy_run = latest_workflow_run(repo, args.deploy_workflow)
    stale_runs = find_stale_deploy_runs(repo, args.deploy_workflow, git_state["originMain"])
    dry_run = args.dry_run or not args.cancel_stale_deploys
    cancellation_results = cancel_stale_runs(repo, stale_runs, dry_run=dry_run)

    blockers: list[str] = []
    if not git_state["headMatchesOriginMain"]:
        blockers.append("Local HEAD does not match origin/main")
    if not git_state["workingTreeClean"]:
        blockers.append("Working tree has uncommitted changes")
    blockers.extend(evaluate_run(validate_run, git_state["originMain"], "Validate"))
    blockers.extend(evaluate_run(deploy_run, git_state["originMain"], "Deploy to Cloudflare"))
    if stale_runs:
        blockers.append(f"{len(stale_runs)} stale Deploy to Cloudflare run(s) can block production deployment")

    optional_checks: dict[str, Any] = {}
    if args.check_production_health:
        if not args.health_url:
            raise ToolError("--health-url is required with --check-production-health")
        health_cmd = production_health_command(args)
        optional_checks["productionHealth"] = run_optional_check(health_cmd)
        if not optional_checks["productionHealth"]["ok"]:
            blockers.append("Production health check failed")

    if args.check_production_smoke:
        if not args.health_url:
            raise ToolError("--health-url is required with --check-production-smoke")
        smoke_cmd = production_smoke_command(args)
        optional_checks["productionSmoke"] = run_optional_check(smoke_cmd)
        if not optional_checks["productionSmoke"]["ok"]:
            blockers.append("Production smoke check failed")

    summary = {
        "ok": not blockers,
        "dryRun": dry_run,
        "repository": repo,
        "git": git_state,
        "workflows": {
            "validate": {
                "workflow": args.validate_workflow,
                "latestRun": validate_run,
                "problems": evaluate_run(validate_run, git_state["originMain"], "Validate"),
            },
            "deploy": {
                "workflow": args.deploy_workflow,
                "latestRun": deploy_run,
                "problems": evaluate_run(deploy_run, git_state["originMain"], "Deploy to Cloudflare"),
            },
        },
        "staleDeployRuns": stale_runs,
        "cancellations": cancellation_results,
        "optionalChecks": optional_checks,
        "blockers": blockers,
    }
    return summary, 0 if not blockers else 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check whether local release state is synchronized with GitHub deploys.")
    parser.add_argument("--repo", help="GitHub repository in owner/name form. Defaults to gh repo view.")
    parser.add_argument("--validate-workflow", default="ci.yml", help="Validate workflow file name or id.")
    parser.add_argument("--deploy-workflow", default="cloudflare-deploy.yml", help="Deploy workflow file name or id.")
    parser.add_argument(
        "--cancel-stale-deploys",
        action="store_true",
        help="Cancel stale main-branch deploy runs for old SHAs. Never approves production deploys.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Only report actions. This is the default.")
    parser.add_argument("--check-production-health", action="store_true", help="Run deployed /api/health validation.")
    parser.add_argument("--check-production-smoke", action="store_true", help="Run deployed public smoke checks.")
    parser.add_argument("--health-url", help="Production Worker /api/health URL for optional checks.")
    parser.add_argument("--manifest", type=Path, help="Local manifest whose counts must match deployed production.")
    parser.add_argument(
        "--max-cache-age-hours",
        type=float,
        default=36,
        help="Production health/smoke checks fail when the deployed seed is older than this many hours.",
    )
    parser.add_argument(
        "--allow-offline-seed",
        action="store_true",
        help="Allow production checks to pass when the deployed manifest reports an offline seed build.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        summary, exit_code = build_summary(args)
    except ToolError as exc:
        summary = {"ok": False, "errorType": "tool_or_config", "error": str(exc)}
        exit_code = 2

    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
