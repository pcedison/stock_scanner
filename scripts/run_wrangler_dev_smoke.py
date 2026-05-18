from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT_DIR / ".tmp" / "wrangler-dev-smoke.log"


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


def _fetch_json(url: str) -> dict[str, Any]:
    request = Request(url, headers={"Origin": "https://stock-scanner-beta.pages.dev"})
    try:
        with urlopen(request, timeout=5) as response:
            if response.status != 200:
                raise RuntimeError(f"{url} returned HTTP {response.status}")
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{url} returned HTTP {exc.code}: {body[:1000]}") from exc


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

    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [
                npx,
                "wrangler",
                "dev",
                "--config",
                "cloudflare/wrangler.toml",
                "--local",
                "--ip",
                "127.0.0.1",
                "--port",
                str(port),
                "--persist-to",
                ".tmp/wrangler-dev-smoke-state",
                "--log-level",
                "log",
                "--show-interactive-dev-session=false",
            ],
            cwd=ROOT_DIR,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
        )

        deadline = time.monotonic() + timeout_seconds
        url = f"http://127.0.0.1:{port}/api/health"
        try:
            last_error: Exception | None = None
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(f"wrangler dev exited early with code {process.returncode}")
                try:
                    return validate_worker_smoke_payload(_fetch_json(url))
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
