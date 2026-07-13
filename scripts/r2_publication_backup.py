from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

MAX_ATTEMPTS = 5
IMMUTABLE_GENERATION_PREFIX = "public/market_scan/v2/"
SAFE_KEY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*\Z")
MISSING_PATTERN = re.compile(r"(?:\b404\b|NoSuchKey|The specified key does not exist\.)", re.IGNORECASE)


class PublicationError(RuntimeError):
    pass


@dataclass(frozen=True)
class PlanItem:
    object_key: str
    file_path: Path


@dataclass(frozen=True)
class BackupEntry:
    object_key: str
    state: str
    backup_path: str | None


CommandRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]
Sleeper = Callable[[float], None]


def validate_object_key(value: object) -> str:
    if not isinstance(value, str) or not SAFE_KEY_PATTERN.fullmatch(value):
        raise PublicationError("unsafe R2 object key")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise PublicationError("unsafe R2 object key")
    return value


def _validate_bucket(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{1,62}", value):
        raise PublicationError("unsafe R2 bucket name")
    return value


def is_immutable_generation_key(key: str) -> bool:
    return key.startswith(IMMUTABLE_GENERATION_PREFIX)


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PublicationError(f"unable to read publication metadata: {path.name}") from exc


def load_plan(path: Path) -> list[PlanItem]:
    payload = _load_json(path)
    if not isinstance(payload, list) or not payload:
        raise PublicationError("R2 upload plan must be a non-empty list")
    items: list[PlanItem] = []
    seen: set[str] = set()
    for raw in payload:
        if not isinstance(raw, dict):
            raise PublicationError("R2 upload plan contains an invalid item")
        key = validate_object_key(raw.get("object_key"))
        file_path = raw.get("file_path")
        if not isinstance(file_path, str) or not file_path:
            raise PublicationError(f"R2 upload plan has no source file for {key}")
        if key in seen:
            raise PublicationError(f"R2 upload plan contains duplicate key: {key}")
        seen.add(key)
        items.append(PlanItem(key, Path(file_path)))
    return items


def _default_runner(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )


def _run_with_retry(
    command: list[str],
    key: str,
    *,
    allow_missing: bool,
    runner: CommandRunner,
    sleeper: Sleeper,
) -> str:
    delay = 5.0
    for attempt in range(1, MAX_ATTEMPTS + 1):
        result = runner(command)
        if result.returncode == 0:
            return "ok"
        output = f"{result.stdout or ''}\n{result.stderr or ''}"
        if allow_missing and MISSING_PATTERN.search(output):
            return "missing"
        if attempt < MAX_ATTEMPTS:
            sleeper(delay)
            delay *= 2
    raise PublicationError(f"R2 operation failed for {key} after {MAX_ATTEMPTS} attempts")


def _local_backup_path(backup_dir: Path, key: str) -> tuple[Path, str]:
    relative = Path("objects", *PurePosixPath(key).parts)
    destination = backup_dir / relative
    try:
        destination.resolve().relative_to(backup_dir.resolve())
    except ValueError as exc:
        raise PublicationError("unsafe backup path") from exc
    return destination, relative.as_posix()


def _require_manifest_inside_backup(backup_dir: Path, manifest_path: Path) -> None:
    try:
        manifest_path.resolve().relative_to(backup_dir.resolve())
    except ValueError as exc:
        raise PublicationError("backup manifest must be inside the backup directory") from exc


def backup_publication(
    plan_path: Path,
    bucket: str,
    backup_dir: Path,
    manifest_path: Path,
    *,
    runner: CommandRunner = _default_runner,
    sleeper: Sleeper = time.sleep,
) -> None:
    bucket = _validate_bucket(bucket)
    items = load_plan(plan_path)
    _require_manifest_inside_backup(backup_dir, manifest_path)
    backup_dir.mkdir(parents=True, exist_ok=True)
    manifest_path.unlink(missing_ok=True)
    entries: list[BackupEntry] = []
    for item in items:
        if is_immutable_generation_key(item.object_key):
            continue
        destination, relative = _local_backup_path(backup_dir, item.object_key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.unlink(missing_ok=True)
        result = _run_with_retry(
            [
                "npx",
                "wrangler",
                "r2",
                "object",
                "get",
                f"{bucket}/{item.object_key}",
                "--remote",
                "--file",
                str(destination),
            ],
            item.object_key,
            allow_missing=True,
            runner=runner,
            sleeper=sleeper,
        )
        if result == "missing":
            destination.unlink(missing_ok=True)
            entries.append(BackupEntry(item.object_key, "absent", None))
            continue
        if not destination.is_file():
            raise PublicationError(f"R2 backup did not create a file for {item.object_key}")
        entries.append(BackupEntry(item.object_key, "present", relative))

    payload = {
        "schema_version": 1,
        "bucket": bucket,
        "entries": [entry.__dict__ for entry in entries],
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(manifest_path)


def publish_plan(
    plan_path: Path,
    bucket: str,
    *,
    runner: CommandRunner = _default_runner,
    sleeper: Sleeper = time.sleep,
) -> None:
    bucket = _validate_bucket(bucket)
    for item in load_plan(plan_path):
        if not item.file_path.is_file():
            raise PublicationError(f"R2 publication source is missing for {item.object_key}")
        _run_with_retry(
            [
                "npx",
                "wrangler",
                "r2",
                "object",
                "put",
                f"{bucket}/{item.object_key}",
                "--remote",
                "--file",
                str(item.file_path),
            ],
            item.object_key,
            allow_missing=False,
            runner=runner,
            sleeper=sleeper,
        )


def _load_backup_entries(backup_dir: Path, manifest_path: Path, bucket: str) -> list[BackupEntry]:
    _require_manifest_inside_backup(backup_dir, manifest_path)
    payload = _load_json(manifest_path)
    if not isinstance(payload, dict) or payload.get("schema_version") != 1 or payload.get("bucket") != bucket:
        raise PublicationError("invalid R2 backup manifest identity")
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list):
        raise PublicationError("invalid R2 backup manifest entries")
    entries: list[BackupEntry] = []
    seen: set[str] = set()
    for raw in raw_entries:
        if not isinstance(raw, dict):
            raise PublicationError("invalid R2 backup manifest entry")
        key = validate_object_key(raw.get("object_key"))
        state = raw.get("state")
        backup_path = raw.get("backup_path")
        if key in seen or is_immutable_generation_key(key) or state not in {"present", "absent"}:
            raise PublicationError("invalid R2 backup manifest entry")
        seen.add(key)
        _destination, expected_relative = _local_backup_path(backup_dir, key)
        if state == "present":
            if backup_path != expected_relative:
                raise PublicationError("unsafe backup path")
            if not (backup_dir / Path(*PurePosixPath(expected_relative).parts)).is_file():
                raise PublicationError(f"R2 backup file is missing for {key}")
        elif backup_path is not None:
            raise PublicationError("invalid absent-object backup entry")
        entries.append(BackupEntry(key, state, backup_path))
    return entries


def restore_publication(
    backup_dir: Path,
    manifest_path: Path,
    bucket: str,
    *,
    runner: CommandRunner = _default_runner,
    sleeper: Sleeper = time.sleep,
) -> None:
    bucket = _validate_bucket(bucket)
    entries = _load_backup_entries(backup_dir, manifest_path, bucket)
    failures: list[str] = []
    for entry in entries:
        if entry.state == "present":
            assert entry.backup_path is not None
            source = backup_dir / Path(*PurePosixPath(entry.backup_path).parts)
            command = [
                "npx",
                "wrangler",
                "r2",
                "object",
                "put",
                f"{bucket}/{entry.object_key}",
                "--remote",
                "--file",
                str(source),
            ]
            allow_missing = False
        else:
            command = [
                "npx",
                "wrangler",
                "r2",
                "object",
                "delete",
                f"{bucket}/{entry.object_key}",
                "--remote",
            ]
            allow_missing = True
        try:
            _run_with_retry(
                command,
                entry.object_key,
                allow_missing=allow_missing,
                runner=runner,
                sleeper=sleeper,
            )
        except PublicationError:
            failures.append(entry.object_key)
    if failures:
        raise PublicationError(f"R2 rollback failed for {len(failures)} object(s)")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Safely publish mutable Cloudflare R2 seed objects.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    backup = subparsers.add_parser("backup")
    backup.add_argument("--plan-json", type=Path, required=True)
    backup.add_argument("--bucket", required=True)
    backup.add_argument("--backup-dir", type=Path, required=True)
    backup.add_argument("--manifest", type=Path, required=True)
    publish = subparsers.add_parser("publish")
    publish.add_argument("--plan-json", type=Path, required=True)
    publish.add_argument("--bucket", required=True)
    restore = subparsers.add_parser("restore")
    restore.add_argument("--bucket", required=True)
    restore.add_argument("--backup-dir", type=Path, required=True)
    restore.add_argument("--manifest", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "backup":
            backup_publication(args.plan_json, args.bucket, args.backup_dir, args.manifest)
        elif args.command == "publish":
            publish_plan(args.plan_json, args.bucket)
        else:
            restore_publication(args.backup_dir, args.manifest, args.bucket)
    except PublicationError as exc:
        print(f"R2 publication safety check failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
