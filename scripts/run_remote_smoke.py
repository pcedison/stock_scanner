from __future__ import annotations

import argparse
import json
import re
import sys
import time
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import HTTPCookieProcessor, Request, build_opener

try:
    from check_cloudflare_health import validate_health_payload
except ModuleNotFoundError:  # Imported as scripts.run_remote_smoke under pytest.
    from scripts.check_cloudflare_health import validate_health_payload


REMOTE_SMOKE_USER_AGENT = "Mozilla/5.0 stock-scanner-remote-smoke/1.0"


def base_url_from_health_url(health_url: str) -> str:
    parsed = urlparse(health_url)
    if parsed.scheme != "https" or not parsed.netloc or not parsed.path.endswith("/api/health"):
        raise RuntimeError("health URL must be an https URL ending in /api/health")
    base_path = parsed.path[: -len("/api/health")]
    return f"{parsed.scheme}://{parsed.netloc}{base_path}/"


def _load_json_file(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    return json.loads(path.read_text(encoding="utf-8"))


class RemoteClient:
    def __init__(self, base_url: str, timeout: int):
        self.base_url = base_url
        self.timeout = timeout
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))

    def request_json(self, path: str, method: str = "GET", payload: dict[str, Any] | None = None) -> dict[str, Any]:
        status, data = self.request_json_status(path, method=method, payload=payload)
        if status >= 400:
            raise RuntimeError(f"{method} {path} returned HTTP {status}: {json.dumps(data, ensure_ascii=False)[:500]}")
        return data

    def request_json_status(
        self,
        path: str,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "User-Agent": REMOTE_SMOKE_USER_AGENT,
            "X-Stock-Scanner-CSRF": "1",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(urljoin(self.base_url, path.lstrip("/")), data=body, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")
            try:
                return exc.code, json.loads(body_text)
            except json.JSONDecodeError as json_exc:
                raise RuntimeError(f"{method} {path} returned HTTP {exc.code}: {body_text[:500]}") from json_exc
        except URLError as exc:
            raise RuntimeError(f"{method} {path} failed to connect: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{method} {path} did not return valid JSON") from exc


def validate_public_smoke_payloads(payloads: dict[str, dict[str, Any]]) -> dict[str, Any]:
    problems: list[str] = []
    health = payloads.get("health") or {}
    app_status = payloads.get("appStatus") or {}
    data_sources = payloads.get("dataSources") or {}
    market_index = payloads.get("marketIndex") or {}
    auth_me = payloads.get("authMe") or {}
    bad_login = payloads.get("badLogin") or {}
    runtime_config = payloads.get("runtimeConfig") or {}

    if health.get("runtime") != "cloudflare-python-worker":
        problems.append("health.runtime is not cloudflare-python-worker")
    if auth_me.get("authenticated") is not False or auth_me.get("user") is not None:
        problems.append("auth/me without a session should return authenticated=false")
    if bad_login.get("status") != 401:
        problems.append(f"bad auth/login returned HTTP {bad_login.get('status')}, expected 401")
    if not isinstance(app_status.get("dataSourceStatus"), dict):
        problems.append("app-status is missing dataSourceStatus")
    if not isinstance(app_status.get("schedulerAutoScan"), dict):
        problems.append("app-status is missing schedulerAutoScan")
    if not data_sources.get("activeProvider"):
        problems.append("data-sources status is missing activeProvider")
    generation_id = market_index.get("generationId")
    if not isinstance(generation_id, str) or not re.fullmatch(r"[0-9a-f]{24}", generation_id):
        problems.append("market index is missing a valid generationId")
    counts = market_index.get("counts")
    categories = counts.get("categories") if isinstance(counts, dict) else None
    if not isinstance(categories, dict):
        problems.append("market index is missing per-category counts")
        market_rows = 0
    else:
        market_rows = sum(
            value for value in (categories.get(category) for category in ("entry", "watch", "excluded"))
            if isinstance(value, int)
        )
    if market_rows < 1000:
        problems.append(f"market index reports only {market_rows} rows")
    if not isinstance(market_index.get("cacheStatus"), dict):
        problems.append("market index is missing cacheStatus")
    if runtime_config.get("marketScanApiVersion") != "v2":
        problems.append("runtime-config must advertise marketScanApiVersion=v2")
    if not isinstance(runtime_config.get("edgeCacheEnabled"), bool):
        problems.append("runtime-config is missing edgeCacheEnabled")
    if problems:
        raise RuntimeError("; ".join(problems))
    return {
        "healthStatus": health.get("status"),
        "runtime": health.get("runtime"),
        "activeProvider": data_sources.get("activeProvider"),
        "schedulerAction": app_status.get("schedulerAutoScan", {}).get("action"),
        "marketScanRows": market_rows,
        "marketGenerationId": generation_id,
        "marketScanApiVersion": runtime_config.get("marketScanApiVersion"),
        "edgeCacheEnabled": runtime_config.get("edgeCacheEnabled"),
    }


def run_public_smoke(
    health_url: str,
    manifest: Path | None,
    timeout: int,
    propagation_timeout: int = 90,
    max_cache_age_hours: float | None = None,
    reject_offline_seed: bool = False,
    max_refresh_delay_minutes: float | None = None,
) -> dict[str, Any]:
    base_url = base_url_from_health_url(health_url)
    client = RemoteClient(base_url, timeout)
    expected_manifest = _load_json_file(manifest)
    deadline = time.monotonic() + propagation_timeout
    last_error: RuntimeError | None = None

    while True:
        try:
            health = client.request_json("/api/health")
            validate_health_payload(
                health,
                expected_manifest,
                max_cache_age_hours=max_cache_age_hours,
                max_refresh_delay_minutes=max_refresh_delay_minutes,
                reject_offline_seed=reject_offline_seed,
            )
            payloads = {
                "health": health,
                "authMe": client.request_json("/api/auth/me"),
                "badLogin": {
                    "status": client.request_json_status(
                        "/api/auth/login",
                        method="POST",
                        payload={"username": "remote-smoke-not-real@example.com", "password": "not-the-password"},
                    )[0],
                },
                "appStatus": client.request_json("/api/app-status"),
                "runtimeConfig": client.request_json("/api/runtime-config"),
                "dataSources": client.request_json("/api/data-sources/status"),
                "marketIndex": client.request_json("/api/scan/market/index"),
            }
            return validate_public_smoke_payloads(payloads)
        except RuntimeError as exc:
            last_error = exc
            if time.monotonic() >= deadline:
                raise last_error from exc
            time.sleep(3)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run deployed Cloudflare public smoke checks.")
    parser.add_argument("--health-url", required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--propagation-timeout", type=int, default=90)
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
    args = parser.parse_args(argv)

    try:
        summary = run_public_smoke(
            args.health_url,
            args.manifest,
            args.timeout,
            args.propagation_timeout,
            max_cache_age_hours=args.max_cache_age_hours,
            max_refresh_delay_minutes=args.max_refresh_delay_minutes,
            reject_offline_seed=args.reject_offline_seed,
        )
    except RuntimeError as exc:
        print(f"Remote smoke failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
