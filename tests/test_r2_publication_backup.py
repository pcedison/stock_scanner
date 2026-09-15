from __future__ import annotations

import hashlib
import json
import subprocess
import threading
from pathlib import Path

import pytest

import scripts.r2_publication_backup as publication


def _result(returncode: int = 0, *, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def _write_plan(path: Path, items: list[tuple[str, Path]]) -> None:
    path.write_text(
        json.dumps([{"object_key": key, "file_path": source.as_posix()} for key, source in items]),
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    "key",
    ("", "/public/a.json", "../a.json", "public/../a.json", "public\\a.json", "public//a.json", "C:/a.json"),
)
def test_validate_object_key_rejects_unsafe_paths(key):
    with pytest.raises(publication.PublicationError, match="unsafe R2 object key"):
        publication.validate_object_key(key)


def test_immutable_generation_objects_are_not_mutable_publication_state():
    assert publication.is_immutable_generation_key("public/market_scan/v2/" + "a" * 24 + "/index.json") is True
    assert publication.is_immutable_generation_key("public/market_scan_index.json") is False
    assert publication.is_immutable_generation_key("public/manifest.json") is False


def test_backup_records_present_and_explicitly_missing_objects_but_skips_immutable(tmp_path):
    source = tmp_path / "source.json"
    source.write_text("{}", encoding="utf-8")
    plan = tmp_path / "plan.json"
    immutable = "public/market_scan/v2/" + "a" * 24 + "/index.json"
    _write_plan(
        plan,
        [
            (immutable, source),
            ("public/companies.json", source),
            ("public/market_scan_index.json", source),
        ],
    )
    backup_dir = tmp_path / "backup"
    manifest = backup_dir / "manifest.json"
    commands: list[list[str]] = []

    def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        key = command[5].split("/", 1)[1]
        if key == "public/market_scan_index.json":
            return _result(1, stderr="HTTP 404 NoSuchKey")
        destination = Path(command[command.index("--file") + 1])
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"previous")
        return _result()

    publication.backup_publication(plan, "bucket", backup_dir, manifest, runner=runner, sleeper=lambda _: None)

    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert [entry["object_key"] for entry in payload["entries"]] == [
        "public/companies.json",
        "public/market_scan_index.json",
    ]
    assert [entry["state"] for entry in payload["entries"]] == ["present", "absent"]
    assert all(immutable not in " ".join(command) for command in commands)
    assert (backup_dir / "objects" / "public" / "companies.json").read_bytes() == b"previous"


def test_backup_accepts_wranglers_exact_missing_key_message(tmp_path):
    source = tmp_path / "source.json"
    source.write_text("{}", encoding="utf-8")
    plan = tmp_path / "plan.json"
    _write_plan(plan, [("public/market_scan_index.json", source)])
    manifest = tmp_path / "backup" / "manifest.json"

    publication.backup_publication(
        plan,
        "bucket",
        tmp_path / "backup",
        manifest,
        runner=lambda _command: _result(1, stderr="The specified key does not exist."),
        sleeper=lambda _: None,
    )

    entry = json.loads(manifest.read_text(encoding="utf-8"))["entries"][0]
    assert entry == {
        "backup_path": None,
        "object_key": "public/market_scan_index.json",
        "state": "absent",
    }


def test_backup_fails_closed_after_five_unknown_failures(tmp_path):
    source = tmp_path / "source.json"
    source.write_text("{}", encoding="utf-8")
    plan = tmp_path / "plan.json"
    _write_plan(plan, [("public/companies.json", source)])
    attempts = 0

    def runner(_command: list[str]) -> subprocess.CompletedProcess[str]:
        nonlocal attempts
        attempts += 1
        return _result(1, stderr="temporary transport failure")

    manifest = tmp_path / "backup" / "manifest.json"
    with pytest.raises(publication.PublicationError, match="after 5 attempts"):
        publication.backup_publication(
            plan,
            "bucket",
            tmp_path / "backup",
            manifest,
            runner=runner,
            sleeper=lambda _: None,
        )

    assert attempts == 5
    assert not manifest.exists()


def test_publish_uploads_every_object_retries_and_puts_manifest_last(tmp_path):
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    manifest_file = tmp_path / "manifest.json"
    first.write_text("1", encoding="utf-8")
    second.write_text("2", encoding="utf-8")
    manifest_file.write_text("m", encoding="utf-8")
    plan = tmp_path / "plan.json"
    _write_plan(
        plan,
        [
            ("public/first.json", first),
            ("public/second.json", second),
            ("public/manifest.json", manifest_file),
        ],
    )
    attempts: dict[str, int] = {}
    successful: list[str] = []
    lock = threading.Lock()

    def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        key = command[5].split("/", 1)[1]
        with lock:
            attempts[key] = attempts.get(key, 0) + 1
            attempt = attempts[key]
        if key == "public/first.json" and attempt == 1:
            return _result(1, stderr="temporary")
        with lock:
            successful.append(key)
        return _result()

    publication.publish_plan(plan, "bucket", runner=runner, sleeper=lambda _: None)

    assert set(successful) == {"public/first.json", "public/second.json", "public/manifest.json"}
    assert attempts == {"public/first.json": 2, "public/second.json": 1, "public/manifest.json": 1}
    assert successful[-1] == "public/manifest.json"


def test_publish_skips_unchanged_objects_and_orders_pointer_before_manifest_last(tmp_path):
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    generation_index = tmp_path / "gen_index.json"
    generation_entry = tmp_path / "gen_entry.json"
    pointer_file = tmp_path / "pointer.json"
    manifest_file = tmp_path / "manifest.json"
    a.write_text("unchanged", encoding="utf-8")
    b.write_text("changed-new", encoding="utf-8")
    generation_index.write_text("gen-index", encoding="utf-8")
    generation_entry.write_text("gen-entry", encoding="utf-8")
    pointer_file.write_text("pointer", encoding="utf-8")
    manifest_file.write_text("manifest", encoding="utf-8")

    plan = tmp_path / "plan.json"
    _write_plan(
        plan,
        [
            ("public/a.json", a),
            ("public/b.json", b),
            ("public/market_scan/v2/g/index.json", generation_index),
            ("public/market_scan/v2/g/entry/0.json", generation_entry),
            ("public/market_scan_index.json", pointer_file),
            ("public/manifest.json", manifest_file),
        ],
    )

    backup_dir = tmp_path / "backup"
    manifest_path = backup_dir / "manifest.json"
    (backup_dir / "objects" / "public").mkdir(parents=True)
    (backup_dir / "objects" / "public" / "a.json").write_bytes(b"unchanged")
    (backup_dir / "objects" / "public" / "b.json").write_bytes(b"old-b-contents")
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "bucket": "bucket",
                "entries": [
                    {
                        "object_key": "public/a.json",
                        "state": "present",
                        "backup_path": "objects/public/a.json",
                        "sha256": hashlib.sha256(b"unchanged").hexdigest(),
                    },
                    {
                        "object_key": "public/b.json",
                        "state": "present",
                        "backup_path": "objects/public/b.json",
                        "sha256": hashlib.sha256(b"old-b-contents").hexdigest(),
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    commands: list[list[str]] = []
    lock = threading.Lock()

    def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        with lock:
            commands.append(command)
        return _result()

    publication.publish_plan(
        plan,
        "bucket",
        backup_dir=backup_dir,
        manifest_path=manifest_path,
        runner=runner,
        sleeper=lambda _: None,
    )

    put_commands = [command for command in commands if command[4] == "put"]
    put_keys = [command[5].split("/", 1)[1] for command in put_commands]
    assert set(put_keys) == {
        "public/b.json",
        "public/market_scan/v2/g/index.json",
        "public/market_scan/v2/g/entry/0.json",
        "public/market_scan_index.json",
        "public/manifest.json",
    }
    generation_page_indexes = [i for i, key in enumerate(put_keys) if key.startswith("public/market_scan/v2/")]
    pointer_index = put_keys.index("public/market_scan_index.json")
    assert generation_page_indexes and max(generation_page_indexes) < pointer_index
    assert put_keys[-1] == "public/manifest.json"


def test_publish_fails_closed_when_one_upload_keeps_failing(tmp_path):
    a = tmp_path / "a.json"
    x = tmp_path / "x.json"
    pointer_file = tmp_path / "pointer.json"
    manifest_file = tmp_path / "manifest.json"
    a.write_text("a", encoding="utf-8")
    x.write_text("x", encoding="utf-8")
    pointer_file.write_text("p", encoding="utf-8")
    manifest_file.write_text("m", encoding="utf-8")
    plan = tmp_path / "plan.json"
    _write_plan(
        plan,
        [
            ("public/a.json", a),
            ("public/x.json", x),
            ("public/market_scan_index.json", pointer_file),
            ("public/manifest.json", manifest_file),
        ],
    )

    commands: list[list[str]] = []
    lock = threading.Lock()

    def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        with lock:
            commands.append(command)
        key = command[5].split("/", 1)[1]
        if key == "public/x.json":
            return _result(1, stderr="permanent failure")
        return _result()

    with pytest.raises(publication.PublicationError, match="public/x.json"):
        publication.publish_plan(plan, "bucket", runner=runner, sleeper=lambda _: None)

    assert all(command[5] != "bucket/public/market_scan_index.json" for command in commands)
    assert all(command[5] != "bucket/public/manifest.json" for command in commands)


def test_publish_without_backup_manifest_uploads_everything(tmp_path):
    a = tmp_path / "a.json"
    manifest_file = tmp_path / "manifest.json"
    a.write_text("a", encoding="utf-8")
    manifest_file.write_text("m", encoding="utf-8")
    plan = tmp_path / "plan.json"
    _write_plan(plan, [("public/a.json", a), ("public/manifest.json", manifest_file)])

    commands: list[list[str]] = []
    lock = threading.Lock()

    def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        with lock:
            commands.append(command)
        return _result()

    publication.publish_plan(plan, "bucket", runner=runner, sleeper=lambda _: None)

    put_keys = {command[5].split("/", 1)[1] for command in commands}
    assert put_keys == {"public/a.json", "public/manifest.json"}
    assert commands[-1][5] == "bucket/public/manifest.json"


def test_backup_records_sha256_of_present_objects(tmp_path):
    source = tmp_path / "source.json"
    source.write_text("{}", encoding="utf-8")
    plan = tmp_path / "plan.json"
    _write_plan(
        plan,
        [
            ("public/companies.json", source),
            ("public/market_scan_index.json", source),
        ],
    )
    backup_dir = tmp_path / "backup"
    manifest = backup_dir / "manifest.json"

    def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        key = command[5].split("/", 1)[1]
        if key == "public/market_scan_index.json":
            return _result(1, stderr="HTTP 404 NoSuchKey")
        destination = Path(command[command.index("--file") + 1])
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"hello")
        return _result()

    publication.backup_publication(plan, "bucket", backup_dir, manifest, runner=runner, sleeper=lambda _: None)

    payload = json.loads(manifest.read_text(encoding="utf-8"))
    entries = {entry["object_key"]: entry for entry in payload["entries"]}
    assert entries["public/companies.json"]["sha256"] == hashlib.sha256(b"hello").hexdigest()
    assert "sha256" not in entries["public/market_scan_index.json"]


def test_load_backup_entries_rejects_malformed_sha256(tmp_path):
    backup_dir = tmp_path / "backup"
    present = backup_dir / "objects" / "public" / "companies.json"
    present.parent.mkdir(parents=True)
    present.write_bytes(b"data")
    manifest = backup_dir / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "bucket": "bucket",
                "entries": [
                    {
                        "object_key": "public/companies.json",
                        "state": "present",
                        "backup_path": "objects/public/companies.json",
                        "sha256": "nope",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(publication.PublicationError):
        publication._load_backup_entries(backup_dir, manifest, "bucket")


def test_restore_reinstates_present_objects_and_deletes_new_objects(tmp_path):
    backup_dir = tmp_path / "backup"
    previous = backup_dir / "objects" / "public" / "companies.json"
    previous.parent.mkdir(parents=True)
    previous.write_bytes(b"previous")
    manifest = backup_dir / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "bucket": "bucket",
                "entries": [
                    {
                        "object_key": "public/companies.json",
                        "state": "present",
                        "backup_path": "objects/public/companies.json",
                    },
                    {
                        "object_key": "public/market_scan_index.json",
                        "state": "absent",
                        "backup_path": None,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    commands: list[list[str]] = []
    lock = threading.Lock()

    def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        with lock:
            commands.append(command)
        return _result()

    publication.restore_publication(backup_dir, manifest, "bucket", runner=runner, sleeper=lambda _: None)

    put_commands = [command for command in commands if command[4] == "put"]
    delete_commands = [command for command in commands if command[4] == "delete"]
    assert len(put_commands) == 1
    assert put_commands[0][3:6] == ["object", "put", "bucket/public/companies.json"]
    assert len(delete_commands) == 1
    assert delete_commands[0][3:6] == ["object", "delete", "bucket/public/market_scan_index.json"]


def test_restore_rejects_tampered_backup_path_before_running_commands(tmp_path):
    backup_dir = tmp_path / "backup"
    backup_dir.mkdir()
    manifest = backup_dir / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "bucket": "bucket",
                "entries": [
                    {
                        "object_key": "public/companies.json",
                        "state": "present",
                        "backup_path": "../outside.json",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    commands: list[list[str]] = []

    def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return _result()

    with pytest.raises(publication.PublicationError, match="unsafe backup path"):
        publication.restore_publication(
            backup_dir,
            manifest,
            "bucket",
            runner=runner,
            sleeper=lambda _: None,
        )

    assert commands == []
