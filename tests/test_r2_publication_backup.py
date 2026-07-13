from __future__ import annotations

import json
import subprocess
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


def test_publish_preserves_plan_order_and_retries(tmp_path):
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_text("1", encoding="utf-8")
    second.write_text("2", encoding="utf-8")
    plan = tmp_path / "plan.json"
    _write_plan(plan, [("public/first.json", first), ("public/second.json", second)])
    attempts: dict[str, int] = {}
    successful: list[str] = []

    def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        key = command[5].split("/", 1)[1]
        attempts[key] = attempts.get(key, 0) + 1
        if key == "public/first.json" and attempts[key] == 1:
            return _result(1, stderr="temporary")
        successful.append(key)
        return _result()

    publication.publish_plan(plan, "bucket", runner=runner, sleeper=lambda _: None)

    assert successful == ["public/first.json", "public/second.json"]
    assert attempts == {"public/first.json": 2, "public/second.json": 1}


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

    def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return _result()

    publication.restore_publication(backup_dir, manifest, "bucket", runner=runner, sleeper=lambda _: None)

    assert commands[0][3:6] == ["object", "put", "bucket/public/companies.json"]
    assert commands[1][3:6] == ["object", "delete", "bucket/public/market_scan_index.json"]


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
