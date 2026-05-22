from __future__ import annotations

import re
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
        key=lambda p: _dated_seed_key(p),
        reverse=True,
    )
    return candidates[0] if candidates else data_dir / "official_cache_seed_latest.zip"
