from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_WRANGLER = ROOT_DIR / "cloudflare" / "wrangler.toml"


def parse_secret_names(output: str) -> set[str]:
    start = output.find("[")
    if start < 0:
        raise ValueError("wrangler secret list did not return a JSON array")
    payload = json.loads(output[start:])
    if not isinstance(payload, list):
        raise ValueError("wrangler secret list did not return a JSON array")
    return {str(item.get("name") or "").strip() for item in payload if isinstance(item, dict) and item.get("name")}


def missing_secret_names(available: set[str], required: list[str]) -> list[str]:
    return [name for name in required if name not in available]


def wrangler_secret_list_command(config: Path) -> list[str]:
    npx = shutil.which("npx") or shutil.which("npx.cmd")
    if not npx:
        raise FileNotFoundError("npx is required to inspect Cloudflare Worker secrets")
    return [npx, "wrangler", "secret", "list", "--config", str(config)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify required Cloudflare Worker secret bindings exist.")
    parser.add_argument("names", nargs="+", help="Required Worker secret names")
    parser.add_argument("--config", type=Path, default=DEFAULT_WRANGLER, help="Path to wrangler.toml")
    args = parser.parse_args(argv)

    try:
        command = wrangler_secret_list_command(args.config)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    completed = subprocess.run(command, check=False, text=True, encoding="utf-8", capture_output=True)
    if completed.returncode != 0:
        print(completed.stderr.strip() or completed.stdout.strip(), file=sys.stderr)
        return completed.returncode

    try:
        available = parse_secret_names(completed.stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"Could not parse wrangler secret list output: {exc}", file=sys.stderr)
        return 1

    missing = missing_secret_names(available, args.names)
    if missing:
        print(f"Missing {len(missing)} required Cloudflare Worker secret binding(s).", file=sys.stderr)
        return 1

    print(f"Cloudflare Worker secret bindings present: {len(args.names)} required binding(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
