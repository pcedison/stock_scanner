from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

CHECK_USER_AGENT = "Mozilla/5.0 stock-scanner-health-check/1.0"


def validate_health_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc or not parsed.path.endswith("/api/health"):
        raise RuntimeError("Health URL must be an https URL ending in /api/health")
    return url


def _load_json_url(url: str, timeout: int) -> dict[str, Any]:
    safe_url = validate_health_url(url)
    request = Request(safe_url, headers={"User-Agent": CHECK_USER_AGENT})
    try:
        # validate_health_url restricts scheme/shape before urlopen.
        with urlopen(request, timeout=timeout) as response:  # nosec B310
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError(f"Health check returned HTTP {exc.code}: {safe_url}") from exc
    except URLError as exc:
        raise RuntimeError(f"Health check failed to connect: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Health check did not return valid JSON: {safe_url}") from exc


def _load_json_file(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Manifest is not valid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"Manifest must be a JSON object: {path}")
    return payload


def _counts(payload: dict[str, Any]) -> dict[str, int]:
    raw_counts = payload.get("counts")
    if not isinstance(raw_counts, dict):
        return {}
    return {str(key): int(value or 0) for key, value in raw_counts.items()}


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


DISPATCH_UNHEALTHY_ATTEMPTS = 3


# What to actually do about a failed dispatch, keyed by the code the Worker reports.
DISPATCH_FAILURE_HINTS = {
    "GITHUB_APP_NOT_CONFIGURED": " - set the GITHUB_APP_ID, GITHUB_APP_INSTALLATION_ID"
    " and GITHUB_APP_PRIVATE_KEY Worker secrets",
    "GITHUB_APP_SIGN_FAILED": " - GITHUB_APP_PRIVATE_KEY is not a readable PKCS#8 PEM",
    "GITHUB_APP_TOKEN_HTTP_401": " - the GitHub App private key was revoked or does not match GITHUB_APP_ID",
    "GITHUB_APP_TOKEN_HTTP_403": " - the GitHub App is suspended or blocked from this repository",
    "GITHUB_APP_TOKEN_HTTP_404": " - GITHUB_APP_INSTALLATION_ID is wrong or the app was uninstalled",
    "GITHUB_APP_TOKEN_MALFORMED": " - GitHub returned no token; check the app installation",
    "GITHUB_HTTP_401": " - GitHub rejected the app installation token; check GITHUB_APP_ID"
    " and GITHUB_APP_PRIVATE_KEY",
    "GITHUB_HTTP_403": " - the GitHub App installation is missing Actions: read and write",
    "GITHUB_HTTP_404": " - the refresh workflow file was renamed or the app cannot see the repository",
}


def dispatch_problems(refresh_dispatch: Any) -> list[str]:
    """Problems with the Worker-cron -> GitHub dispatch path reported by /api/health.

    A ``failed`` dispatch is either a 4xx from GitHub (renamed workflow -> 404, bad
    inputs -> 422, installation missing Actions write -> 403) or a GitHub App credential
    fault. Neither ever succeeds on retry without human action, so it fails
    the monitor at once instead of waiting for the seed to go stale.
    """
    if not isinstance(refresh_dispatch, dict):
        return ["refreshDispatch is missing from health payload (Worker is not reporting cron dispatch state)"]
    if refresh_dispatch.get("enabled") is not True:
        return ["refresh dispatch is disabled on the deployed Worker (GITHUB_DISPATCH_ENABLED is not true)"]
    status = refresh_dispatch.get("dispatchStatus")
    code = refresh_dispatch.get("dispatchErrorCode") or "unknown"
    attempts = int(refresh_dispatch.get("dispatchAttempts") or 0)
    if status == "failed":
        hint = DISPATCH_FAILURE_HINTS.get(code, "")
        return [f"refresh dispatch failed: {code}{hint}"]
    if status == "unknown" and attempts >= DISPATCH_UNHEALTHY_ATTEMPTS:
        return [f"refresh dispatch has not been acknowledged after {attempts} attempts: {code}"]
    return []


def validate_health_payload(
    payload: dict[str, Any],
    expected_manifest: dict[str, Any] | None = None,
    *,
    max_cache_age_hours: float | None = None,
    max_refresh_delay_minutes: float | None = None,
    now: datetime | None = None,
    reject_offline_seed: bool = False,
    require_dispatch_healthy: bool = False,
) -> dict[str, Any]:
    problems: list[str] = []
    cache_quality = payload.get("cacheQuality")
    cache_quality_ok = isinstance(cache_quality, dict) and cache_quality.get("ok") is True
    if not cache_quality_ok:
        problems.append("cacheQuality.ok is not true")

    cache = payload.get("cache")
    if not isinstance(cache, dict):
        problems.append("cache manifest is missing from health payload")
        cache = {}

    deployed_counts = _counts(cache)
    expected_counts = _counts(expected_manifest or {})
    if expected_counts and deployed_counts != expected_counts:
        problems.append(f"deployed manifest counts {deployed_counts} do not match expected {expected_counts}")

    if reject_offline_seed:
        quality_gates = cache.get("qualityGates") if isinstance(cache, dict) else {}
        build_mode = str((quality_gates or {}).get("buildMode") or "").strip().lower()
        if build_mode == "offline":
            problems.append("deployed cache was rebuilt from the offline seed zip")

    financial_freshness = cache.get("financialFreshness") if isinstance(cache, dict) else None
    if not isinstance(financial_freshness, dict):
        problems.append("financial freshness is missing from deployed cache manifest")
    elif financial_freshness.get("blocksDeployment") is True:
        problems.append(
            "financial freshness is blocking deployment: "
            f"expected {financial_freshness.get('expectedFinancialPeriod')}, "
            f"latest cached {financial_freshness.get('latestCachedFinancialPeriod')}"
        )

    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    current = current.astimezone(UTC)

    refresh_delay_minutes = None
    cache_status = payload.get("cacheStatus")
    has_cache_status = "cacheStatus" in payload
    policy_stale = False
    policy_degraded_allowed = False
    if has_cache_status:
        if not isinstance(cache_status, dict):
            problems.append("cache refresh policy status is missing or invalid")
        else:
            reported_stale = cache_status.get("isStale") is True
            policy_stale = reported_stale
            if not isinstance(cache_status.get("isStale"), bool):
                problems.append("cache refresh policy isStale is missing or invalid")
            next_refresh_after = _parse_timestamp(cache_status.get("nextRefreshAfter"))
            if next_refresh_after is None:
                problems.append("cache refresh policy nextRefreshAfter is missing or invalid")
            else:
                policy_stale = policy_stale or current >= next_refresh_after
            if policy_stale and next_refresh_after is not None:
                refresh_delay_minutes = max(0.0, (current - next_refresh_after).total_seconds() / 60)
                policy_degraded_allowed = (
                    max_refresh_delay_minutes is not None
                    and refresh_delay_minutes <= max_refresh_delay_minutes
                    and cache_quality_ok
                )
                if not policy_degraded_allowed:
                    limit = "strict" if max_refresh_delay_minutes is None else f"{max_refresh_delay_minutes:g}m"
                    problems.append(
                        "cache refresh policy is stale: "
                        f"next refresh {next_refresh_after.isoformat()} "
                        f"({refresh_delay_minutes:.1f}m delayed, limit {limit})"
                    )

    status = payload.get("status")
    expected_status = "degraded" if policy_stale or not cache_quality_ok else "ok"
    if status != expected_status:
        problems.append(f"health status is {status!r}, expected {expected_status!r}")

    cache_age_hours = None
    checked_at = None
    if max_cache_age_hours is not None:
        checked_at = _parse_timestamp(cache.get("sourceLastCheckedAt") or cache.get("generatedAt"))
        if checked_at is None:
            problems.append("cache sourceLastCheckedAt/generatedAt is missing or invalid")
        else:
            cache_age_hours = max(0.0, (current - checked_at).total_seconds() / 3600)
            if cache_age_hours > max_cache_age_hours:
                problems.append(
                    "deployed cache is stale: "
                    f"last checked {checked_at.isoformat()} "
                    f"({cache_age_hours:.1f}h old, limit {max_cache_age_hours:g}h)"
                )

    refresh_dispatch = payload.get("refreshDispatch")
    if require_dispatch_healthy:
        problems.extend(dispatch_problems(refresh_dispatch))

    if problems:
        raise RuntimeError("; ".join(problems))

    return {
        "status": payload.get("status"),
        "refreshDispatch": refresh_dispatch,
        "runtime": payload.get("runtime"),
        "counts": deployed_counts,
        "cacheQuality": cache_quality,
        "sourceLastCheckedAt": cache.get("sourceLastCheckedAt") or cache.get("generatedAt"),
        "cacheAgeHours": cache_age_hours,
        "refreshDelayMinutes": refresh_delay_minutes,
        "financialFreshness": financial_freshness,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate deployed Cloudflare Worker /api/health output.")
    parser.add_argument("--url", required=True, help="Full deployed /api/health URL")
    parser.add_argument("--manifest", type=Path, help="Local manifest whose counts must match the deployed health payload")
    parser.add_argument(
        "--max-cache-age-hours",
        type=float,
        help="Fail when cache.sourceLastCheckedAt or generatedAt is older than this many hours",
    )
    parser.add_argument(
        "--max-refresh-delay-minutes",
        type=float,
        help="Allow this many minutes after cacheStatus.nextRefreshAfter before failing",
    )
    parser.add_argument(
        "--reject-offline-seed",
        action="store_true",
        help="Fail when the deployed manifest reports qualityGates.buildMode=offline",
    )
    parser.add_argument(
        "--require-dispatch-healthy",
        action="store_true",
        help="Fail when the Worker reports a failed or repeatedly unacknowledged GitHub workflow dispatch",
    )
    parser.add_argument("--timeout", type=int, default=20)
    args = parser.parse_args(argv)

    try:
        expected_manifest = _load_json_file(args.manifest) if args.manifest else None
        summary = validate_health_payload(
            _load_json_url(args.url, args.timeout),
            expected_manifest,
            max_cache_age_hours=args.max_cache_age_hours,
            max_refresh_delay_minutes=args.max_refresh_delay_minutes,
            reject_offline_seed=args.reject_offline_seed,
            require_dispatch_healthy=args.require_dispatch_healthy,
        )
    except RuntimeError as exc:
        print(f"Cloudflare health check failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
