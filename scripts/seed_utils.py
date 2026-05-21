from __future__ import annotations

from pathlib import Path


def find_seed_zip(data_dir: Path) -> Path:
    """Return the most recently dated official_cache_seed_*.zip in data_dir.

    Selects by filename sort (descending) so the newest dated zip wins.
    Falls back to a sentinel path that will not exist — callers should check
    .exists() when the zip is optional, or let downstream code raise clearly.
    """
    candidates = sorted(data_dir.glob("official_cache_seed_*.zip"), key=lambda p: p.name, reverse=True)
    return candidates[0] if candidates else data_dir / "official_cache_seed_latest.zip"
