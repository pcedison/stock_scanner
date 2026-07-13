from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT_DIR / ".tmp" / "wrangler-dev-smoke.log"
DEFAULT_SMOKE_SUPER_USER = "wrangler-smoke-admin@example.com"
DEFAULT_PERSIST_DIR = ROOT_DIR / ".tmp" / "wrangler-dev-smoke-state"
LOCAL_SMOKE_HOSTS = {"127.0.0.1", "localhost", "::1"}


def validate_worker_smoke_payload(payload: dict[str, Any]) -> dict[str, Any]:
    problems: list[str] = []
    if payload.get("status") not in {"ok", "degraded"}:
        problems.append(f"unexpected health status: {payload.get('status')!r}")
    if payload.get("runtime") != "cloudflare-python-worker":
        problems.append(f"unexpected runtime: {payload.get('runtime')!r}")
    if not isinstance(payload.get("cacheQuality"), dict):
        problems.append("cacheQuality is missing")
    if not isinstance(payload.get("cache"), dict):
        problems.append("cache manifest is missing")
    if problems:
        raise RuntimeError("; ".join(problems))
    return {
        "status": payload.get("status"),
        "runtime": payload.get("runtime"),
        "cacheQualityOk": payload.get("cacheQuality", {}).get("ok"),
    }


def validate_local_smoke_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "http" or parsed.hostname not in LOCAL_SMOKE_HOSTS:
        raise RuntimeError("Wrangler dev smoke URL must be an http loopback URL")
    return url


def _request_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    expected_status: int = 200,
    send_origin: bool = True,
) -> tuple[int, dict[str, str], dict[str, Any]]:
    safe_url = validate_local_smoke_url(url)
    request_headers = dict(headers or {})
    if send_origin:
        request_headers = {"Origin": "https://stock-scanner-beta.pages.dev", **request_headers}
    request = Request(safe_url, data=body, headers=request_headers, method=method)
    try:
        # validate_local_smoke_url restricts requests to loopback HTTP.
        with urlopen(request, timeout=5) as response:  # nosec B310
            payload = json.loads(response.read().decode("utf-8") or "{}")
            response_headers = {key.lower(): value for key, value in response.headers.items()}
            if response.status != expected_status:
                raise RuntimeError(f"{safe_url} returned HTTP {response.status}: {str(payload)[:1000]}")
            return response.status, response_headers, payload
    except HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="replace")
        if exc.code == expected_status:
            return exc.code, {key.lower(): value for key, value in exc.headers.items()}, json.loads(response_body or "{}")
        raise RuntimeError(f"{safe_url} returned HTTP {exc.code}: {response_body[:1000]}") from exc


def _fetch_json(url: str) -> dict[str, Any]:
    return _request_json(url)[2]


def _d1_database_name(config_path: Path) -> str:
    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    databases = config.get("d1_databases") or []
    if not databases:
        raise RuntimeError("wrangler config has no D1 database binding")
    return str(databases[0]["database_name"])


def apply_local_migrations(npx: str, config_path: Path, persist_dir: Path) -> None:
    database = _d1_database_name(config_path)
    completed = subprocess.run(
        [
            npx,
            "wrangler",
            "d1",
            "migrations",
            "apply",
            database,
            "--local",
            "--config",
            str(config_path),
            "--persist-to",
            str(persist_dir),
        ],
        cwd=ROOT_DIR,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout).strip()[-2000:])


def build_wrangler_dev_command(npx: str, port: int, persist_dir: Path) -> list[str]:
    return [
        npx,
        "wrangler",
        "dev",
        "--config",
        "cloudflare/wrangler.toml",
        "--local",
        "--test-scheduled",
        "--ip",
        "127.0.0.1",
        "--port",
        str(port),
        "--persist-to",
        str(persist_dir),
        "--log-level",
        "log",
        "--show-interactive-dev-session=false",
        "--var",
        f"SUPER_USER_USERNAME:{os.getenv('SUPER_USER_USERNAME', DEFAULT_SMOKE_SUPER_USER)}",
        "--var",
        "GITHUB_DISPATCH_ENABLED:false",
    ]


def validate_refresh_smoke(base_url: str) -> dict[str, Any]:
    key = "wrangler-smoke-refresh"
    command_url = f"{base_url}/api/scan/market/refresh"
    first_status, first_headers, first = _request_json(
        command_url,
        method="POST",
        headers={"Idempotency-Key": key, "x-stock-scanner-csrf": "1"},
        body=b"",
        expected_status=202,
    )
    second_status, second_headers, second = _request_json(
        command_url,
        method="POST",
        headers={"Idempotency-Key": key, "x-stock-scanner-csrf": "1"},
        body=b"",
        expected_status=202,
    )
    job_id = first.get("jobId")
    if first_status != 202 or second_status != 202 or not isinstance(job_id, str) or len(job_id) != 32:
        raise RuntimeError("refresh command did not return a valid 202 job")
    if second.get("jobId") != job_id:
        raise RuntimeError("refresh command did not reuse the same idempotency key")
    if first_headers.get("cache-control") != "no-store" or second_headers.get("cache-control") != "no-store":
        raise RuntimeError("refresh command must be no-store")
    if first_headers.get("location") != first.get("statusUrl"):
        raise RuntimeError("refresh command Location does not match statusUrl")

    status_code, status_headers, status = _request_json(f"{base_url}{first['statusUrl']}")
    if status_code != 200 or status_headers.get("cache-control") != "no-store":
        raise RuntimeError("refresh status did not return HTTP 200 no-store")
    if status.get("jobId") != job_id:
        raise RuntimeError("refresh status returned the wrong job")

    _request_json(f"{base_url}/api/scan/market/refresh/{'A' * 32}", expected_status=404)
    _request_json(f"{base_url}/cdn-cgi/handler/scheduled?format=json", send_origin=False)
    return {"refreshJobId": job_id, "refreshStatus": status.get("status")}


def _stop_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], check=False, capture_output=True)
    else:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


def run_smoke(port: int, timeout_seconds: int, log_path: Path) -> dict[str, Any]:
    npx = shutil.which("npx")
    if not npx:
        raise RuntimeError("npx is required to run wrangler dev smoke")
    config_path = ROOT_DIR / "cloudflare" / "wrangler.toml"
    persist_dir = DEFAULT_PERSIST_DIR
    apply_local_migrations(npx, config_path, persist_dir)

    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            build_wrangler_dev_command(npx, port, persist_dir),
            cwd=ROOT_DIR,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
        )

        deadline = time.monotonic() + timeout_seconds
        base_url = f"http://127.0.0.1:{port}"
        url = f"{base_url}/api/health"
        try:
            last_error: Exception | None = None
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(f"wrangler dev exited early with code {process.returncode}")
                try:
                    summary = validate_worker_smoke_payload(_fetch_json(url))
                    summary.update(validate_refresh_smoke(base_url))
                    return summary
                except (HTTPError, URLError, json.JSONDecodeError, RuntimeError) as exc:
                    last_error = exc
                    time.sleep(1)
            raise RuntimeError(f"Timed out waiting for {url}: {last_error}")
        finally:
            _stop_process(process)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a local wrangler dev smoke test against the Python Worker.")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--timeout", type=int, default=45)
    parser.add_argument("--log", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)

    try:
        summary = run_smoke(args.port, args.timeout, args.log)
    except RuntimeError as exc:
        print(f"Wrangler dev smoke failed: {exc}", file=sys.stderr)
        if args.log.exists():
            print(args.log.read_text(encoding="utf-8", errors="replace")[-4000:], file=sys.stderr)
        return 1

    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
