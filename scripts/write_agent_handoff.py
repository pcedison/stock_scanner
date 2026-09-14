from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

try:
    from check_release_sync import ROOT_DIR, ToolError, build_summary
except ModuleNotFoundError:  # Imported as scripts.write_agent_handoff under pytest.
    from scripts.check_release_sync import ROOT_DIR, ToolError, build_summary


DEFAULT_JSON_OUTPUT = ROOT_DIR / ".tmp" / "agent_handoff.json"
DEFAULT_MARKDOWN_OUTPUT = ROOT_DIR / ".tmp" / "agent_handoff.md"


def _run_line(run: dict[str, Any] | None) -> str:
    if not run:
        return "missing"
    return f"{run.get('status')}/{run.get('conclusion')} sha={run.get('headSha')} url={run.get('htmlUrl')}"


def handoff_markdown(payload: dict[str, Any]) -> str:
    sync = payload.get("releaseSync", {})
    git = sync.get("git", {})
    workflows = sync.get("workflows", {})
    validate_run = ((workflows.get("validate") or {}).get("latestRun"))
    deploy_run = ((workflows.get("deploy") or {}).get("latestRun"))
    blockers = sync.get("blockers") or []
    optional_checks = sync.get("optionalChecks") or {}

    lines = [
        "# Agent Handoff",
        "",
        f"- Generated: {payload.get('generatedAt')}",
        f"- Repository: {sync.get('repository')}",
        f"- Branch: {git.get('branch')}",
        f"- Local HEAD: {git.get('localHead')}",
        f"- Origin main: {git.get('originMain')}",
        f"- Clean working tree: {git.get('workingTreeClean')}",
        f"- Ahead/behind origin main: {git.get('aheadOfOriginMain')}/{git.get('behindOriginMain')}",
        f"- Validate: {_run_line(validate_run)}",
        f"- Deploy: {_run_line(deploy_run)}",
        f"- Blockers: {', '.join(blockers) if blockers else 'none'}",
    ]
    if optional_checks:
        lines.append("- Optional production checks:")
        for key, result in optional_checks.items():
            lines.append(f"  - {key}: ok={result.get('ok')} rc={result.get('returnCode')}")
    lines.extend(
        [
            "",
            "## Next-agent decision",
            "",
            "Treat this snapshot as evidence, not permission to switch branches, pull,",
            "discard work, cancel runs, deploy, or mutate production.",
            "",
            "- Preserve expected branch and working-tree state.",
            "- If a fresh remote comparison is required, run `git fetch --prune origin`",
            "  and regenerate the snapshot before deciding how to reconcile differences.",
            "- Do not run `git switch` or `git pull` only to make this snapshot green.",
        ]
    )
    return "\n".join(lines) + "\n"


def build_handoff_payload(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    sync_args = SimpleNamespace(
        repo=args.repo,
        validate_workflow=args.validate_workflow,
        deploy_workflow=args.deploy_workflow,
        cancel_stale_deploys=args.cancel_stale_deploys,
        dry_run=args.dry_run,
        check_production_health=args.check_production_health,
        check_production_smoke=args.check_production_smoke,
        health_url=args.health_url,
        manifest=args.manifest,
        max_cache_age_hours=args.max_cache_age_hours,
        allow_offline_seed=args.allow_offline_seed,
    )
    summary, exit_code = build_summary(sync_args)
    return {
        "schemaVersion": 1,
        "generatedAt": datetime.now(UTC).isoformat(),
        "releaseSync": summary,
    }, exit_code


def write_handoff(payload: dict[str, Any], json_output: Path, markdown_output: Path) -> None:
    json_output.parent.mkdir(parents=True, exist_ok=True)
    markdown_output.parent.mkdir(parents=True, exist_ok=True)
    json_output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    markdown_output.write_text(handoff_markdown(payload), encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Write a machine-readable main-release handoff snapshot.")
    parser.add_argument("--repo", help="GitHub repository in owner/name form. Defaults to gh repo view.")
    parser.add_argument("--validate-workflow", default="ci.yml", help="Validate workflow file name or id.")
    parser.add_argument("--deploy-workflow", default="cloudflare-deploy.yml", help="Deploy workflow file name or id.")
    parser.add_argument("--cancel-stale-deploys", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Only report cancellation actions. This is the default.")
    parser.add_argument("--check-production-health", action="store_true")
    parser.add_argument("--check-production-smoke", action="store_true")
    parser.add_argument("--health-url")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--max-cache-age-hours", type=float, default=36)
    parser.add_argument("--allow-offline-seed", action="store_true")
    parser.add_argument("--json-output", type=Path, default=DEFAULT_JSON_OUTPUT)
    parser.add_argument("--markdown-output", type=Path, default=DEFAULT_MARKDOWN_OUTPUT)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        payload, exit_code = build_handoff_payload(args)
        write_handoff(payload, args.json_output, args.markdown_output)
    except ToolError as exc:
        payload = {"schemaVersion": 1, "generatedAt": datetime.now(UTC).isoformat(), "ok": False, "error": str(exc)}
        write_handoff(payload, args.json_output, args.markdown_output)
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 2

    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
