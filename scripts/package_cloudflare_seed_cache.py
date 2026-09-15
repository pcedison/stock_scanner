from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
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
    "reports/market_scan.csv",
    "reports/market_scan.md",
    "analysis_by_code.json",
    "holding_analysis_by_code.json",
)
MARKET_DISCLOSURES = ("announced", "pending")
MARKET_CATEGORIES = ("entry", "watch", "excluded")
GENERATION_ID_RE = re.compile(r"[0-9a-f]{24}")
DISCLOSURE_FIELDS = {"count", *MARKET_CATEGORIES}
CATEGORY_FIELDS = {"count", "pages"}
REFERENCE_FIELDS = {"key", "cursor", "count", "bytes", "sha256"}


def _require_safe_archive_name(arcname: str) -> None:
    if (
        not arcname
        or "\\" in arcname
        or arcname.startswith("/")
        or re.match(r"^[A-Za-z]:", arcname)
        or any(part in {"", ".", ".."} for part in arcname.split("/"))
    ):
        raise ValueError(f"unsafe seed archive path: {arcname}")


def _add_file(archive: zipfile.ZipFile, source: Path, arcname: str, *, allowed_root: Path | None = None) -> None:
    _require_safe_archive_name(arcname)
    if not source.exists():
        raise FileNotFoundError(f"Required seed file is missing: {source}")
    if not source.is_file():
        raise ValueError(f"Required seed source is not a regular file: {source}")
    if allowed_root is not None:
        try:
            source.resolve(strict=True).relative_to(allowed_root.resolve(strict=True))
        except ValueError as exc:
            raise ValueError(f"Required seed source escapes its allowed root: {source}") from exc
    archive.write(source, arcname)


def _fsync_file(path: Path) -> None:
    with path.open("r+b") as handle:
        os.fsync(handle.fileno())


def _write_fsynced_text(path: Path, content: str) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _replace_artifact_pair(zip_temp: Path, sha_temp: Path, zip_path: Path, sha_path: Path) -> None:
    zip_backup = zip_path.with_name(f"{zip_path.name}.rollback")
    sha_backup = sha_path.with_name(f"{sha_path.name}.rollback")
    zip_existed = zip_path.is_file()
    sha_existed = sha_path.is_file()
    if zip_backup.exists() or sha_backup.exists():
        raise RuntimeError(
            "seed recovery backups already exist; refusing to overwrite them before manual recovery"
        )
    cleanup_backups = False
    try:
        if zip_existed:
            shutil.copy2(zip_path, zip_backup)
            _fsync_file(zip_backup)
        if sha_existed:
            shutil.copy2(sha_path, sha_backup)
            _fsync_file(sha_backup)

        # Publish the checksum first. A process crash at this two-file boundary fails closed;
        # synchronous failures are rolled back to the prior matching pair below.
        sha_temp.replace(sha_path)
        zip_temp.replace(zip_path)
        cleanup_backups = True
    except BaseException as publish_error:
        rollback_errors: list[BaseException] = []
        for target, backup, existed in (
            (sha_path, sha_backup, sha_existed),
            (zip_path, zip_backup, zip_existed),
        ):
            try:
                if existed and backup.exists():
                    backup.replace(target)
                elif not existed and target.exists():
                    target.unlink()
            except BaseException as rollback_error:
                rollback_errors.append(rollback_error)
        if rollback_errors:
            raise RuntimeError(
                "seed ZIP/SHA publish failed and rollback was incomplete; rollback backups were retained"
            ) from publish_error
        cleanup_backups = True
        raise
    finally:
        if cleanup_backups:
            for backup in (zip_backup, sha_backup):
                if backup.exists():
                    backup.unlink()


def _require_generation_id(value: object) -> str:
    if not isinstance(value, str) or GENERATION_ID_RE.fullmatch(value) is None:
        raise ValueError("invalid market generation ID")
    return value


def _require_exact_fields(payload: dict[str, object], expected: set[str], label: str) -> None:
    if set(payload) != expected:
        raise ValueError(f"unexpected market index fields in {label}")


def _safe_seed_file(seed_dir: Path, relative_name: str, *, label: str) -> Path:
    if "\\" in relative_name:
        raise ValueError(f"unsafe {label}: {relative_name}")
    relative = Path(relative_name)
    if relative.is_absolute() or ".." in relative.parts or relative.as_posix() != relative_name:
        raise ValueError(f"unsafe {label}: {relative_name}")
    root = seed_dir.resolve()
    source = seed_dir / relative
    if not source.is_file():
        raise FileNotFoundError(f"Required seed file is missing: {source}")
    try:
        source.resolve(strict=True).relative_to(root)
    except ValueError as exc:
        raise ValueError(f"unsafe {label}: {relative_name}") from exc
    return source


def referenced_market_generation_files(seed_dir: Path) -> list[Path]:
    pointer_path = _safe_seed_file(seed_dir, "market_scan_index.json", label="market pointer path")
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("market_scan_index.json is not valid JSON") from exc
    if not isinstance(pointer, dict):
        raise ValueError("market_scan_index.json must be an object")
    generation_id = _require_generation_id(pointer.get("generationId"))
    names = {f"market_scan/v2/{generation_id}/index.json"}
    seen_keys: set[str] = set()
    disclosures = pointer.get("disclosures")
    if not isinstance(disclosures, dict):
        raise ValueError("market_scan_index.json disclosures must be an object")
    _require_exact_fields(disclosures, set(MARKET_DISCLOSURES), "disclosures")
    for disclosure in MARKET_DISCLOSURES:
        disclosure_bucket = disclosures.get(disclosure)
        if not isinstance(disclosure_bucket, dict):
            raise ValueError(f"market_scan_index.json {disclosure} disclosure is missing")
        _require_exact_fields(disclosure_bucket, DISCLOSURE_FIELDS, disclosure)
        for category in MARKET_CATEGORIES:
            bucket = disclosure_bucket.get(category)
            if isinstance(bucket, dict):
                _require_exact_fields(bucket, CATEGORY_FIELDS, f"{disclosure}/{category}")
            refs = bucket.get("pages") if isinstance(bucket, dict) else None
            if not isinstance(refs, list):
                raise ValueError(f"market_scan_index.json {disclosure}/{category} pages must be a list")
            for reference in refs:
                if isinstance(reference, dict):
                    _require_exact_fields(reference, REFERENCE_FIELDS, f"{disclosure}/{category} reference")
                key = reference.get("key") if isinstance(reference, dict) else None
                if not isinstance(key, str):
                    raise ValueError("market page reference key must be a string")
                expected_prefix = f"public/market_scan/v2/{generation_id}/{disclosure}/{category}/"
                suffix = key.removeprefix(expected_prefix)
                if (
                    key in seen_keys
                    or not key.startswith(expected_prefix)
                    or re.fullmatch(r"(?:0|[1-9][0-9]*)\.json", suffix) is None
                    or "\\" in key
                    or ".." in Path(key).parts
                ):
                    raise ValueError(f"unsafe market page path: {key}")
                seen_keys.add(key)
                names.add(key.removeprefix("public/"))
    files = [
        _safe_seed_file(seed_dir, name, label="market page path")
        for name in sorted(names)
    ]
    immutable_index = next(path for path in files if path.as_posix().endswith(f"/{generation_id}/index.json"))
    if pointer_path.read_bytes() != immutable_index.read_bytes():
        raise ValueError("market pointer bytes do not match immutable generation index")
    return files


def package_seed_cache(zip_path: Path = DEFAULT_ZIP, sha_path: Path = DEFAULT_SHA, seed_dir: Path = SEED_DIR) -> str:
    temp_path = zip_path.with_suffix(zip_path.suffix + ".tmp")
    sha_temp_path = sha_path.with_suffix(sha_path.suffix + ".tmp")
    for path in (temp_path, sha_temp_path):
        if path.exists():
            path.unlink()

    try:
        generation_files = referenced_market_generation_files(seed_dir)
        with zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for entry in OFFICIAL_ENTRIES:
                _add_file(
                    archive,
                    ROOT_DIR / "data" / entry,
                    entry,
                    allowed_root=ROOT_DIR / "data",
                )
            for entry in SEED_FILES:
                _add_file(
                    archive,
                    seed_dir / entry,
                    f"cloudflare_seed/{entry}",
                    allowed_root=seed_dir,
                )
            _add_file(
                archive,
                seed_dir / "market_scan_index.json",
                "cloudflare_seed/market_scan_index.json",
                allowed_root=seed_dir,
            )
            for generation_file in generation_files:
                relative = generation_file.resolve().relative_to(seed_dir.resolve()).as_posix()
                _add_file(
                    archive,
                    generation_file,
                    f"cloudflare_seed/{relative}",
                    allowed_root=seed_dir,
                )
            shard_dir = seed_dir / "analysis_shards"
            shard_paths = sorted(shard_dir.glob("*.json"))
            if not shard_paths:
                raise FileNotFoundError(f"Required analysis shards are missing: {shard_dir}")
            for shard_path in shard_paths:
                _add_file(
                    archive,
                    shard_path,
                    f"cloudflare_seed/analysis_shards/{shard_path.name}",
                    allowed_root=seed_dir,
                )
            holding_shard_dir = seed_dir / "holding_analysis_shards"
            holding_shard_paths = sorted(holding_shard_dir.glob("*.json"))
            if not holding_shard_paths:
                raise FileNotFoundError(f"Required holding analysis shards are missing: {holding_shard_dir}")
            for shard_path in holding_shard_paths:
                _add_file(
                    archive,
                    shard_path,
                    f"cloudflare_seed/holding_analysis_shards/{shard_path.name}",
                    allowed_root=seed_dir,
                )

        _fsync_file(temp_path)
        digest = hashlib.sha256(temp_path.read_bytes()).hexdigest().upper()
        _write_fsynced_text(sha_temp_path, f"{digest}  {zip_path.name}\n")
        _replace_artifact_pair(temp_path, sha_temp_path, zip_path, sha_path)
        return digest
    finally:
        for path in (temp_path, sha_temp_path):
            if path.exists():
                path.unlink()


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
