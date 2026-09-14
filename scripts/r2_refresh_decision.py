from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

try:
    from check_cloudflare_health import parse_closed_dates, trading_hours_between
except ModuleNotFoundError:  # Imported as scripts.r2_refresh_decision under pytest.
    from scripts.check_cloudflare_health import parse_closed_dates, trading_hours_between

COUNT_FIELDS = ("pending_count", "cnt")
JOB_CHECK_ERROR = "D1 pending-job query unavailable"


def load_json_value(json_text: str) -> Any:
    try:
        return json.loads(json_text)
    except json.JSONDecodeError as exc:
        raise ValueError("input is not valid JSON") from exc


def load_json_file(path: Path | None) -> Any | None:
    if path is None or not path.exists():
        return None
    return load_json_value(path.read_text(encoding="utf-8"))


def find_count(value: Any, fields: tuple[str, ...] = COUNT_FIELDS) -> int:
    if isinstance(value, dict):
        for field in fields:
            if field in value:
                try:
                    return int(value.get(field) or 0)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"{field} is not an integer") from exc
        for child in value.values():
            found = find_count_or_none(child, fields)
            if found is not None:
                return found
    if isinstance(value, list):
        for child in value:
            found = find_count_or_none(child, fields)
            if found is not None:
                return found
    return 0


def find_count_or_none(value: Any, fields: tuple[str, ...]) -> int | None:
    if isinstance(value, dict):
        for field in fields:
            if field in value:
                try:
                    return int(value.get(field) or 0)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"{field} is not an integer") from exc
        for child in value.values():
            found = find_count_or_none(child, fields)
            if found is not None:
                return found
    if isinstance(value, list):
        for child in value:
            found = find_count_or_none(child, fields)
            if found is not None:
                return found
    return None


def cloudflare_pending_count(value: Any) -> int:
    if not isinstance(value, dict) or value.get("success") is not True:
        raise ValueError(JOB_CHECK_ERROR)

    query_results = value.get("result")
    if not isinstance(query_results, list) or len(query_results) != 1:
        raise ValueError(JOB_CHECK_ERROR)
    query = query_results[0]
    if not isinstance(query, dict) or query.get("success") is not True:
        raise ValueError(JOB_CHECK_ERROR)

    rows = query.get("results")
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise ValueError(JOB_CHECK_ERROR)
    count = rows[0].get("cnt")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError(JOB_CHECK_ERROR)
    return count


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def refresh_due_at(health_payload: dict[str, Any]) -> datetime | None:
    if not isinstance(health_payload, dict):
        return None
    for field in ("cacheStatus", "cache"):
        cache = health_payload.get(field)
        if not isinstance(cache, dict):
            continue
        due_at = parse_timestamp(cache.get("nextRefreshAfter"))
        if due_at is not None:
            return due_at
    return None


def health_age_hours(health_payload: dict[str, Any], now: datetime | None = None) -> float | None:
    """Trading-day hours since the seed was last checked (weekends and closed days excluded)."""
    cache = health_payload.get("cache") if isinstance(health_payload, dict) else None
    cache = cache if isinstance(cache, dict) else {}
    checked_at = parse_timestamp(cache.get("sourceLastCheckedAt") or cache.get("generatedAt"))
    if checked_at is None:
        return None
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    closed_dates = parse_closed_dates(cache.get("marketClosedDates"))
    return trading_hours_between(checked_at, current.astimezone(UTC), closed_dates)


def early_refresh_decision(
    *,
    force: bool,
    pending_count: int,
    health_payload: dict[str, Any] | None,
    health_error: str,
    health_url_configured: bool,
    max_cache_age_hours: float,
    job_check_error: str = "",
    refresh_ahead_minutes: float = 0,
    now: datetime | None = None,
) -> dict[str, Any]:
    if force:
        return {
            "runRefresh": True,
            "pendingCount": "manual",
            "staleRefresh": False,
            "messages": ["Force refresh requested via workflow_dispatch."],
        }

    messages: list[str] = []
    stale_refresh = False
    age_hours = None
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    current = current.astimezone(UTC)

    if job_check_error:
        messages.append(JOB_CHECK_ERROR)
    if health_error:
        stale_refresh = True
        messages.append("Health URL unreachable; treating as stale.")
    elif health_payload is not None:
        status = health_payload.get("status")
        if status is not None and status != "ok":
            stale_refresh = True
            messages.append("Health status is not ok; R2 rebuild will run.")

        cache_status = health_payload.get("cacheStatus")
        if isinstance(cache_status, dict) and cache_status.get("isStale") is True:
            stale_refresh = True
            messages.append("Health cache policy reports stale; R2 rebuild will run.")

        due_at = refresh_due_at(health_payload)
        if due_at is not None and current >= due_at - timedelta(minutes=refresh_ahead_minutes):
            stale_refresh = True
            messages.append("Production seed is within the refresh-ahead window; R2 rebuild will run.")

        age_hours = health_age_hours(health_payload, now=current)
        if age_hours is None:
            if due_at is None:
                stale_refresh = True
                messages.append("Production seed timestamp is missing; R2 rebuild will run.")
        elif age_hours > max_cache_age_hours:
            stale_refresh = True
            messages.append(f"Production seed is stale ({age_hours:.1f}h old); R2 rebuild will run.")
        else:
            messages.append(f"Production seed is fresh ({age_hours:.1f}h old).")
    elif health_url_configured:
        stale_refresh = True
        messages.append("Health URL configured but no payload was captured; treating as stale.")

    if pending_count <= 0 and not stale_refresh and not job_check_error:
        messages.append("No pending refresh jobs and seed is fresh; skipping heavy setup.")

    return {
        "runRefresh": pending_count > 0 or stale_refresh or bool(job_check_error),
        "pendingCount": pending_count,
        "staleRefresh": stale_refresh,
        "cacheAgeHours": age_hours,
        "messages": messages,
    }


def _append_line(path: Path | None, line: str) -> None:
    if path is None:
        return
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def write_github_outputs(decision: dict[str, Any], output_path: Path | None, summary_path: Path | None) -> None:
    _append_line(output_path, f"pending_count={decision['pendingCount']}")
    _append_line(output_path, f"stale_refresh={str(decision['staleRefresh']).lower()}")
    _append_line(output_path, f"run_refresh={str(decision['runRefresh']).lower()}")
    for message in decision.get("messages", []):
        _append_line(summary_path, str(message))


def _fields(value: str | None) -> tuple[str, ...]:
    fields = tuple(item.strip() for item in str(value or "").split(",") if item.strip())
    return fields or COUNT_FIELDS


def run_pending_count(args: argparse.Namespace) -> int:
    payload = load_json_file(args.json_file)
    if payload is None and args.json:
        payload = load_json_value(args.json)
    count = find_count(payload, _fields(args.fields))
    print(count)
    return 0


def run_early_check(args: argparse.Namespace) -> int:
    health_payload = load_json_file(args.health_json_file)
    pending_count = 0
    job_check_error = JOB_CHECK_ERROR if args.job_check_error else ""
    if args.cloudflare_api_json_file is not None:
        try:
            api_payload = load_json_file(args.cloudflare_api_json_file)
            pending_count = cloudflare_pending_count(api_payload)
        except (OSError, UnicodeError, ValueError):
            job_check_error = JOB_CHECK_ERROR
    decision = early_refresh_decision(
        force=args.force,
        pending_count=pending_count,
        health_payload=health_payload if isinstance(health_payload, dict) else None,
        health_error=args.health_error or "",
        job_check_error=job_check_error,
        health_url_configured=args.health_url_configured,
        max_cache_age_hours=args.max_cache_age_hours,
        refresh_ahead_minutes=args.refresh_ahead_minutes,
    )
    write_github_outputs(decision, args.github_output, args.github_step_summary)
    print(json.dumps(decision, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def backfill_summary_markdown(payload: object) -> str:
    """Render a short Markdown summary of an official history backfill result."""
    if not isinstance(payload, dict):
        return "Official history backfill: no result recorded."
    counts = ", ".join(
        f"{label} {int(payload.get(key) or 0)}"
        for key, label in (
            ("requestedCompanies", "requested"),
            ("backfilledCompanies", "backfilled"),
            ("skippedCompanies", "skipped"),
            ("failedCompanies", "failed"),
            ("pendingCompanies", "pending"),
            ("incomeRows", "income rows"),
            ("balanceRows", "balance rows"),
        )
    )
    periods = payload.get("periods")
    period_text = ", ".join(str(item) for item in periods) if isinstance(periods, list) and periods else "n/a"
    status = "complete" if payload.get("completed") else "in progress"
    return f"Official history backfill ({status}; periods {period_text}): {counts}."


def run_backfill_summary(args: argparse.Namespace) -> int:
    payload = None
    if args.json_file is not None and args.json_file.exists():
        try:
            payload = json.loads(args.json_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = None
    print(backfill_summary_markdown(payload))
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Make testable decisions for the Cloudflare R2 seed refresh workflow.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    pending = subparsers.add_parser("pending-count", help="Extract a pending refresh count from JSON.")
    pending.add_argument("--json-file", type=Path)
    pending.add_argument("--json")
    pending.add_argument("--fields", help="Comma-separated count field names to search recursively.")
    pending.set_defaults(func=run_pending_count)

    early = subparsers.add_parser("early-check", help="Write GitHub outputs for the lightweight refresh pre-check.")
    early.add_argument("--force", action="store_true")
    early.add_argument("--cloudflare-api-json-file", type=Path)
    early.add_argument("--health-json-file", type=Path)
    early.add_argument("--health-error", default="")
    early.add_argument("--job-check-error", default="")
    early.add_argument("--health-url-configured", action="store_true")
    early.add_argument("--max-cache-age-hours", type=float, default=36)
    early.add_argument("--refresh-ahead-minutes", type=float, default=0)
    early.add_argument("--github-output", type=Path)
    early.add_argument("--github-step-summary", type=Path)
    early.set_defaults(func=run_early_check)

    backfill = subparsers.add_parser("backfill-summary", help="Summarize an official history backfill result as Markdown.")
    backfill.add_argument("--json-file", type=Path)
    backfill.set_defaults(func=run_backfill_summary)

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return args.func(args)
    except ValueError as exc:
        print(f"R2 refresh decision failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
