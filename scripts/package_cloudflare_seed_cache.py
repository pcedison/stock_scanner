from __future__ import annotations

import argparse
import hashlib
import sys
import zipfile
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
from seed_utils import find_seed_zip  # noqa: E402

DEFAULT_ZIP = find_seed_zip(ROOT_DIR / "data")
DEFAULT_SHA = DEFAULT_ZIP.with_suffix(".sha256")
SEED_DIR = ROOT_DIR / "cloudflare" / "seed"
OFFICIAL_ENTRIES = (
    "official_fundamentals_history.json",
    "official_history_backfill_progress.json",
    "monthly_revenue_history.json",
)
SEED_FILES = (
    "manifest.json",
    "companies.json",
    "data_sources_status.json",
    "market_scan_latest.json",
    "analysis_by_code.json",
    "holding_analysis_by_code.json",
)


def _add_file(archive: zipfile.ZipFile, source: Path, arcname: str) -> None:
    if not source.exists():
        raise FileNotFoundError(f"Required seed file is missing: {source}")
    archive.write(source, arcname)


def package_seed_cache(zip_path: Path = DEFAULT_ZIP, sha_path: Path = DEFAULT_SHA, seed_dir: Path = SEED_DIR) -> str:
    temp_path = zip_path.with_suffix(zip_path.suffix + ".tmp")
    if temp_path.exists():
        temp_path.unlink()

    with zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for entry in OFFICIAL_ENTRIES:
            _add_file(archive, ROOT_DIR / "data" / entry, entry)
        for entry in SEED_FILES:
            _add_file(archive, seed_dir / entry, f"cloudflare_seed/{entry}")
        shard_dir = seed_dir / "analysis_shards"
        shard_paths = sorted(shard_dir.glob("*.json"))
        if not shard_paths:
            raise FileNotFoundError(f"Required analysis shards are missing: {shard_dir}")
        for shard_path in shard_paths:
            _add_file(archive, shard_path, f"cloudflare_seed/analysis_shards/{shard_path.name}")
        holding_shard_dir = seed_dir / "holding_analysis_shards"
        holding_shard_paths = sorted(holding_shard_dir.glob("*.json"))
        if not holding_shard_paths:
            raise FileNotFoundError(f"Required holding analysis shards are missing: {holding_shard_dir}")
        for shard_path in holding_shard_paths:
            _add_file(archive, shard_path, f"cloudflare_seed/holding_analysis_shards/{shard_path.name}")

    digest = hashlib.sha256(temp_path.read_bytes()).hexdigest().upper()
    temp_path.replace(zip_path)
    sha_path.write_text(f"{digest}  {zip_path.name}\n", encoding="utf-8")
    return digest


def main() -> int:
    parser = argparse.ArgumentParser(description="Package official and Cloudflare seed artifacts into the committed seed zip.")
    parser.add_argument("--zip", type=Path, default=DEFAULT_ZIP)
    parser.add_argument("--sha", type=Path, default=DEFAULT_SHA)
    parser.add_argument("--seed-dir", type=Path, default=SEED_DIR)
    args = parser.parse_args()

    digest = package_seed_cache(args.zip, args.sha, args.seed_dir)
    print(f"{digest}  {args.zip.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
