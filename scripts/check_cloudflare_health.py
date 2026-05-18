from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def _load_json_url(url: str, timeout: int) -> dict[str, Any]:
    request = Request(url, headers={"User-Agent": "stock-scanner-health-check/1.0"})
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError(f"Health check returned HTTP {exc.code}: {url}") from exc
    except URLError as exc:
        raise RuntimeError(f"Health check failed to connect: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Health check did not return valid JSON: {url}") from exc


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


def validate_health_payload(payload: dict[str, Any], expected_manifest: dict[str, Any] | None = None) -> dict[str, Any]:
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

    if problems:
        raise RuntimeError("; ".join(problems))

    return {
        "status": payload.get("status"),
        "runtime": payload.get("runtime"),
        "counts": deployed_counts,
        "cacheQuality": cache_quality,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate deployed Cloudflare Worker /api/health output.")
    parser.add_argument("--url", required=True, help="Full deployed /api/health URL")
    parser.add_argument("--manifest", type=Path, help="Local manifest whose counts must match the deployed health payload")
    parser.add_argument("--timeout", type=int, default=20)
    args = parser.parse_args(argv)

    try:
        expected_manifest = _load_json_file(args.manifest) if args.manifest else None
        summary = validate_health_payload(_load_json_url(args.url, args.timeout), expected_manifest)
    except RuntimeError as exc:
        print(f"Cloudflare health check failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
