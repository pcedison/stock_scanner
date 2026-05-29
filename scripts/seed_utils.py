from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path

SEED_ZIP_PATTERN = re.compile(r"^official_cache_seed_(\d{4}-\d{2}-\d{2})\.zip$")


def _dated_seed_key(path: Path) -> date | None:
    match = SEED_ZIP_PATTERN.fullmatch(path.name)
    if not match:
        return None
    try:
        return date.fromisoformat(match.group(1))
    except ValueError:
        return None


def find_seed_zip(data_dir: Path) -> Path:
    """Return the most recently dated official_cache_seed_*.zip in data_dir.

    Selects by the date embedded in the artifact name so the newest dated zip wins.
    Falls back to a sentinel path that will not exist — callers should check
    .exists() when the zip is optional, or let downstream code raise clearly.
    """
    candidates = sorted(
        (path for path in data_dir.glob("official_cache_seed_*.zip") if _dated_seed_key(path) is not None),
        key=_dated_seed_key,
        reverse=True,
    )
    return candidates[0] if candidates else data_dir / "official_cache_seed_latest.zip"


def resolve_seed_zip(data_dir: Path) -> Path:
    """Return the newest dated seed zip, or raise when none exists."""
    seed_zip = find_seed_zip(data_dir)
    if seed_zip.exists() and _dated_seed_key(seed_zip) is not None:
        return seed_zip
    raise FileNotFoundError(f"No dated seed zip found in {data_dir}: expected official_cache_seed_YYYY-MM-DD.zip")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resolve the newest dated committed Cloudflare seed zip.")
    parser.add_argument("data_dir", type=Path, help="Directory containing official_cache_seed_YYYY-MM-DD.zip files")
    parser.add_argument("--print", action="store_true", dest="print_path", help="Print the resolved seed zip path")
    args = parser.parse_args(argv)

    try:
        seed_zip = resolve_seed_zip(args.data_dir)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.print_path:
        print(seed_zip)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
