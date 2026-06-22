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


def validate_health_payload(
    payload: dict[str, Any],
    expected_manifest: dict[str, Any] | None = None,
    *,
    max_cache_age_hours: float | None = None,
    now: datetime | None = None,
    reject_offline_seed: bool = False,
) -> dict[str, Any]:
    problems: list[str] = []
    if payload.get("status") != "ok":
        problems.append(f"health status is {payload.get('status')!r}, expected 'ok'")

    cache_quality = payload.get("cacheQuality")
    if not isinstance(cache_quality, dict) or cache_quality.get("ok") is not True:
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
    if isinstance(financial_freshness, dict) and financial_freshness.get("blocksDeployment") is True:
        problems.append(
            "financial freshness is blocking deployment: "
            f"expected {financial_freshness.get('expectedFinancialPeriod')}, "
            f"latest cached {financial_freshness.get('latestCachedFinancialPeriod')}"
        )

    cache_age_hours = None
    checked_at = None
    if max_cache_age_hours is not None:
        checked_at = _parse_timestamp(cache.get("sourceLastCheckedAt") or cache.get("generatedAt"))
        if checked_at is None:
            problems.append("cache sourceLastCheckedAt/generatedAt is missing or invalid")
        else:
            current = now or datetime.now(UTC)
            if current.tzinfo is None:
                current = current.replace(tzinfo=UTC)
            current = current.astimezone(UTC)
            cache_age_hours = max(0.0, (current - checked_at).total_seconds() / 3600)
            if cache_age_hours > max_cache_age_hours:
                problems.append(
                    "deployed cache is stale: "
                    f"last checked {checked_at.isoformat()} "
                    f"({cache_age_hours:.1f}h old, limit {max_cache_age_hours:g}h)"
                )

    if problems:
        raise RuntimeError("; ".join(problems))

    return {
        "status": payload.get("status"),
        "runtime": payload.get("runtime"),
        "counts": deployed_counts,
        "cacheQuality": cache_quality,
        "sourceLastCheckedAt": cache.get("sourceLastCheckedAt") or cache.get("generatedAt"),
        "cacheAgeHours": cache_age_hours,
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
        "--reject-offline-seed",
        action="store_true",
        help="Fail when the deployed manifest reports qualityGates.buildMode=offline",
    )
    parser.add_argument("--timeout", type=int, default=20)
    args = parser.parse_args(argv)

    try:
        expected_manifest = _load_json_file(args.manifest) if args.manifest else None
        summary = validate_health_payload(
            _load_json_url(args.url, args.timeout),
            expected_manifest,
            max_cache_age_hours=args.max_cache_age_hours,
            reject_offline_seed=args.reject_offline_seed,
        )
    except RuntimeError as exc:
        print(f"Cloudflare health check failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
