from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

try:
    from seed_utils import find_seed_zip
except ModuleNotFoundError:  # Imported as scripts.cloudflare_seed_upload_plan under pytest.
    from scripts.seed_utils import find_seed_zip


PUBLIC_FILES = (
    "manifest.json",
    "companies.json",
    "data_sources_status.json",
    "market_scan_latest.json",
    "market_scan_summary.json",
    "analysis_by_code.json",
    "holding_analysis_by_code.json",
)
REQUIRED_OFFICIAL_FILES = (
    "official_fundamentals_history.json",
    "official_history_backfill_progress.json",
)
OPTIONAL_OFFICIAL_FILES = ("monthly_revenue_history.json",)


@dataclass(frozen=True)
class UploadPlanItem:
    object_key: str
    file_path: str


def _require_file(path: Path) -> Path:
    if not path.is_file():
        raise FileNotFoundError(f"required seed upload file is missing: {path}")
    return path


def build_upload_plan(seed_dir: Path, data_dir: Path) -> list[UploadPlanItem]:
    plan: list[UploadPlanItem] = []
    for name in PUBLIC_FILES:
        path = _require_file(seed_dir / name)
        plan.append(UploadPlanItem(f"public/{name}", path.as_posix()))

    for shard_dir, object_dir in (
        (seed_dir / "analysis_shards", "public/analysis_shards"),
        (seed_dir / "holding_analysis_shards", "public/holding_analysis_shards"),
    ):
        if not shard_dir.is_dir():
            raise FileNotFoundError(f"required shard directory is missing: {shard_dir}")
        for path in sorted(shard_dir.glob("*.json")):
            plan.append(UploadPlanItem(f"{object_dir}/{path.name}", path.as_posix()))

    for name in REQUIRED_OFFICIAL_FILES:
        path = _require_file(data_dir / name)
        plan.append(UploadPlanItem(f"official/{name}", path.as_posix()))

    for name in OPTIONAL_OFFICIAL_FILES:
        path = data_dir / name
        if path.is_file():
            plan.append(UploadPlanItem(f"official/{name}", path.as_posix()))

    seed_zip = find_seed_zip(data_dir)
    if seed_zip.is_file():
        plan.append(UploadPlanItem(f"official/{seed_zip.name}", seed_zip.as_posix()))

    return plan


def write_outputs(plan: list[UploadPlanItem], json_output: Path | None, tsv_output: Path | None) -> None:
    if json_output:
        json_output.parent.mkdir(parents=True, exist_ok=True)
        json_output.write_text(json.dumps([asdict(item) for item in plan], indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if tsv_output:
        tsv_output.parent.mkdir(parents=True, exist_ok=True)
        tsv_output.write_text(
            "".join(f"{item.object_key}\t{item.file_path}\n" for item in plan),
            encoding="utf-8",
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the Cloudflare R2 seed upload manifest.")
    parser.add_argument("--seed-dir", type=Path, default=Path("cloudflare/seed"))
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--output-json", type=Path)
    parser.add_argument("--output-tsv", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        plan = build_upload_plan(args.seed_dir, args.data_dir)
    except FileNotFoundError as exc:
        print(f"Cloudflare seed upload plan failed: {exc}", file=sys.stderr)
        return 1
    write_outputs(plan, args.output_json, args.output_tsv)
    print(json.dumps([asdict(item) for item in plan], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
