from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]


def _load_manifest(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Manifest is not valid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"Manifest must be a JSON object: {path}")
    return payload


def validate_d1_backup(path: Path | None) -> list[str]:
    if path is None:
        return []
    if not path.exists():
        return [f"D1 backup does not exist: {path}"]
    preview = path.read_text(encoding="utf-8", errors="replace")[:4000].upper()
    if "CREATE TABLE" not in preview and "INSERT INTO" not in preview and "PRAGMA" not in preview:
        return [f"D1 backup does not look like a Wrangler SQL export: {path}"]
    return []


def render_recovery_plan(
    *,
    d1_backup: Path | None,
    worker_version: str | None,
    manifest: dict[str, Any],
    bucket: str,
    database: str,
    pages_project: str,
) -> str:
    counts = manifest.get("counts") if isinstance(manifest.get("counts"), dict) else {}
    lines = [
        "# Cloudflare Recovery Plan",
        "",
        "This plan is intentionally non-destructive. Review each command before running it.",
        "",
        "## 1. Freeze and observe",
        "",
        "- Pause scheduled deploys if an incident is active.",
        "- Capture current `/api/health`, Worker logs, and the failed GitHub Actions run URL.",
        "",
        "## 2. Worker rollback",
        "",
    ]
    if worker_version:
        lines.append(
            f"```bash\nnpx wrangler rollback {worker_version} --config cloudflare/wrangler.toml --yes --message \"Manual recovery rollback\"\n```"
        )
    else:
        lines.append("- Find the previous healthy Worker version in Cloudflare dashboard or `wrangler deployments list`.")
        lines.append(
            "```bash\nnpx wrangler rollback <previous-worker-version-id> --config cloudflare/wrangler.toml --yes --message \"Manual recovery rollback\"\n```"
        )

    lines.extend(
        [
            "",
            "## 3. D1 recovery",
            "",
        ]
    )
    if d1_backup:
        lines.append(f"- Candidate backup: `{d1_backup}`")
        lines.append(
            f"```bash\nnpx wrangler d1 execute {database} --remote --config cloudflare/wrangler.toml --file {d1_backup}\n```"
        )
    else:
        lines.append("- Download the `d1-pre-deploy-backup-<run_id>` artifact from the failed deploy run first.")
        lines.append(
            f"```bash\nnpx wrangler d1 execute {database} --remote --config cloudflare/wrangler.toml --file <downloaded-backup.sql>\n```"
        )

    lines.extend(
        [
            "",
            "## 4. R2 seed recovery",
            "",
            f"- Bucket: `{bucket}`",
            f"- Expected manifest counts: `{json.dumps(counts, sort_keys=True)}`",
            "```bash",
            "CLOUDFLARE_SEED_MODE=offline python scripts/build_cloudflare_seed.py",
            f"npx wrangler r2 object put {bucket}/public/manifest.json --remote --file cloudflare/seed/manifest.json",
            f"npx wrangler r2 object put {bucket}/public/companies.json --remote --file cloudflare/seed/companies.json",
            f"npx wrangler r2 object put {bucket}/public/market_scan_latest.json --remote --file cloudflare/seed/market_scan_latest.json",
            f"npx wrangler r2 object put {bucket}/public/analysis_by_code.json --remote --file cloudflare/seed/analysis_by_code.json",
            "```",
            "",
            "## 5. Pages verification",
            "",
            f"- Pages project: `{pages_project}`",
            "- Verify the Pages `/api/*` proxy points at the recovered Worker.",
            "- Run `python scripts/run_remote_smoke.py --health-url \"$CF_WORKER_HEALTH_URL\" --manifest cloudflare/seed/manifest.json`.",
        ]
    )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a non-destructive Cloudflare recovery runbook.")
    parser.add_argument("--d1-backup", type=Path)
    parser.add_argument("--worker-version")
    parser.add_argument("--manifest", type=Path, default=ROOT_DIR / "cloudflare" / "seed" / "manifest.json")
    parser.add_argument("--bucket", default="stock-scanner-beta-cache")
    parser.add_argument("--database", default="stock-scanner-beta-db")
    parser.add_argument("--pages-project", default="stock-scanner-beta")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    problems = validate_d1_backup(args.d1_backup)
    if problems:
        print("Recovery plan validation failed:", file=sys.stderr)
        for problem in problems:
            print(f"- {problem}", file=sys.stderr)
        return 1

    try:
        manifest = _load_manifest(args.manifest if args.manifest.exists() else None)
        plan = render_recovery_plan(
            d1_backup=args.d1_backup,
            worker_version=args.worker_version,
            manifest=manifest,
            bucket=args.bucket,
            database=args.database,
            pages_project=args.pages_project,
        )
    except RuntimeError as exc:
        print(f"Recovery plan validation failed: {exc}", file=sys.stderr)
        return 1

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(plan, encoding="utf-8")
    else:
        print(plan)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
