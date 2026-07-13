from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

import scripts.package_cloudflare_seed_cache as package_module
from backend.services.market_query import build_market_generation, canonical_json_bytes


def _write_package_inputs(tmp_path: Path) -> tuple[Path, Path, str, set[str]]:
    seed_dir = tmp_path / "cloudflare" / "seed"
    data_dir = tmp_path / "data"
    seed_dir.mkdir(parents=True)
    data_dir.mkdir()

    scan = {
        "generatedAt": "2026-07-13T00:00:00+00:00",
        "filingContext": {"freshnessFinancialReport": {"period": "2026Q1"}},
        "entry": [
            {
                "stockCode": "2330",
                "status": "ENTRY",
                "reasons": [{"code": "OFFICIAL_Q", "severity": "INFO", "message": "2026Q1"}],
            }
        ],
        "watch": [{"stockCode": "2317", "status": "INSUFFICIENT_DATA", "reasons": []}],
        "excluded": [],
    }
    generation = build_market_generation(scan)
    for object_key, content in generation.files.items():
        target = seed_dir / object_key.removeprefix("public/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    (seed_dir / "market_scan_index.json").write_bytes(canonical_json_bytes(generation.index))
    orphan = seed_dir / "market_scan" / "v2" / ("f" * 24) / "index.json"
    orphan.parent.mkdir(parents=True)
    orphan.write_text("{}", encoding="utf-8")

    for name in package_module.SEED_FILES:
        (seed_dir / name).write_text("{}", encoding="utf-8")
    (seed_dir / "analysis_shards").mkdir()
    (seed_dir / "analysis_shards" / "23.json").write_text("{}", encoding="utf-8")
    (seed_dir / "holding_analysis_shards").mkdir()
    (seed_dir / "holding_analysis_shards" / "23.json").write_text("{}", encoding="utf-8")
    for name in package_module.OFFICIAL_ENTRIES:
        (data_dir / name).write_text("{}", encoding="utf-8")

    expected_generation_entries = {
        f"cloudflare_seed/{key.removeprefix('public/')}" for key in generation.files
    }
    return seed_dir, data_dir, generation.generation_id, expected_generation_entries


def test_package_contains_only_pointer_referenced_generation(tmp_path, monkeypatch):
    seed_dir, data_dir, generation_id, expected_generation_entries = _write_package_inputs(tmp_path)
    monkeypatch.setattr(package_module, "ROOT_DIR", tmp_path)
    archive_path = data_dir / "seed.zip"

    package_module.package_seed_cache(archive_path, data_dir / "seed.sha256", seed_dir)

    with zipfile.ZipFile(archive_path) as archive:
        names = set(archive.namelist())
    packaged_v2 = {name for name in names if name.startswith("cloudflare_seed/market_scan/v2/")}
    assert packaged_v2 == expected_generation_entries
    assert f"cloudflare_seed/market_scan/v2/{generation_id}/index.json" in names
    assert "cloudflare_seed/market_scan_index.json" in names
    assert not any(("f" * 24) in name for name in names)


def test_package_rejects_unsafe_pointer_page_path(tmp_path, monkeypatch):
    seed_dir, data_dir, _generation_id, _expected_generation_entries = _write_package_inputs(tmp_path)
    monkeypatch.setattr(package_module, "ROOT_DIR", tmp_path)
    pointer_path = seed_dir / "market_scan_index.json"
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    pointer["disclosures"]["announced"]["entry"]["pages"][0]["key"] = "public/../../secret.json"
    pointer_path.write_bytes(canonical_json_bytes(pointer))

    with pytest.raises(ValueError, match="unsafe market page path"):
        package_module.package_seed_cache(data_dir / "seed.zip", data_dir / "seed.sha256", seed_dir)


def test_package_rejects_pointer_and_immutable_index_byte_mismatch(tmp_path, monkeypatch):
    seed_dir, data_dir, generation_id, _expected_generation_entries = _write_package_inputs(tmp_path)
    monkeypatch.setattr(package_module, "ROOT_DIR", tmp_path)
    immutable_index = seed_dir / "market_scan" / "v2" / generation_id / "index.json"
    immutable_index.write_bytes(immutable_index.read_bytes() + b"\n")

    with pytest.raises(ValueError, match="pointer bytes do not match immutable generation index"):
        package_module.package_seed_cache(data_dir / "seed.zip", data_dir / "seed.sha256", seed_dir)


def test_package_rejects_referenced_symlink_that_escapes_seed_root(tmp_path, monkeypatch):
    seed_dir, data_dir, _generation_id, _expected_generation_entries = _write_package_inputs(tmp_path)
    monkeypatch.setattr(package_module, "ROOT_DIR", tmp_path)
    pointer = json.loads((seed_dir / "market_scan_index.json").read_text(encoding="utf-8"))
    reference = pointer["disclosures"]["announced"]["entry"]["pages"][0]
    page_path = seed_dir / str(reference["key"]).removeprefix("public/")
    outside = tmp_path / "outside-page.json"
    outside.write_bytes(page_path.read_bytes())
    page_path.unlink()
    try:
        page_path.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(ValueError, match="unsafe market page path"):
        package_module.package_seed_cache(data_dir / "seed.zip", data_dir / "seed.sha256", seed_dir)


@pytest.mark.parametrize("extra_kind", ["disclosure", "category", "reference"])
def test_package_rejects_extra_market_index_structure(tmp_path, monkeypatch, extra_kind):
    seed_dir, data_dir, _generation_id, _expected_generation_entries = _write_package_inputs(tmp_path)
    monkeypatch.setattr(package_module, "ROOT_DIR", tmp_path)
    pointer_path = seed_dir / "market_scan_index.json"
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    if extra_kind == "disclosure":
        pointer["disclosures"]["archived"] = {}
    elif extra_kind == "category":
        pointer["disclosures"]["announced"]["bonus"] = {"count": 0, "pages": []}
    else:
        pointer["disclosures"]["announced"]["entry"]["pages"][0]["unexpected"] = True
    pointer_path.write_bytes(canonical_json_bytes(pointer))

    with pytest.raises(ValueError, match="unexpected market index fields"):
        package_module.package_seed_cache(data_dir / "seed.zip", data_dir / "seed.sha256", seed_dir)


def test_package_sha_preparation_failure_preserves_old_pair_and_cleans_temps(tmp_path, monkeypatch):
    seed_dir, data_dir, _generation_id, _expected_generation_entries = _write_package_inputs(tmp_path)
    monkeypatch.setattr(package_module, "ROOT_DIR", tmp_path)
    archive_path = data_dir / "seed.zip"
    sha_path = data_dir / "seed.sha256"
    archive_path.write_bytes(b"old-zip")
    sha_path.write_text("OLD-SHA  seed.zip\n", encoding="utf-8")

    def fail_sha_write(_path, _content):
        raise OSError("injected SHA preparation failure")

    monkeypatch.setattr(package_module, "_write_fsynced_text", fail_sha_write, raising=False)

    with pytest.raises(OSError, match="injected SHA preparation failure"):
        package_module.package_seed_cache(archive_path, sha_path, seed_dir)

    assert archive_path.read_bytes() == b"old-zip"
    assert sha_path.read_text(encoding="utf-8") == "OLD-SHA  seed.zip\n"
    assert not list(data_dir.glob("*.tmp"))
    assert not list(data_dir.glob("*.rollback"))


def test_package_second_pair_replace_failure_rolls_back_both_files(tmp_path, monkeypatch):
    seed_dir, data_dir, _generation_id, _expected_generation_entries = _write_package_inputs(tmp_path)
    monkeypatch.setattr(package_module, "ROOT_DIR", tmp_path)
    archive_path = data_dir / "seed.zip"
    sha_path = data_dir / "seed.sha256"
    archive_path.write_bytes(b"old-zip")
    sha_path.write_text("OLD-SHA  seed.zip\n", encoding="utf-8")
    real_replace = Path.replace
    formal_replacements = 0

    def fail_second_formal_replace(source, target):
        nonlocal formal_replacements
        if Path(target) in {archive_path, sha_path}:
            formal_replacements += 1
            if formal_replacements == 2:
                raise OSError("injected second replace failure")
        return real_replace(source, target)

    monkeypatch.setattr(Path, "replace", fail_second_formal_replace)

    with pytest.raises(OSError, match="injected second replace failure"):
        package_module.package_seed_cache(archive_path, sha_path, seed_dir)

    assert archive_path.read_bytes() == b"old-zip"
    assert sha_path.read_text(encoding="utf-8") == "OLD-SHA  seed.zip\n"
    assert not list(data_dir.glob("*.tmp"))
    assert not list(data_dir.glob("*.rollback"))


def test_package_rollback_failure_retains_recovery_backups(tmp_path, monkeypatch):
    seed_dir, data_dir, _generation_id, _expected_generation_entries = _write_package_inputs(tmp_path)
    monkeypatch.setattr(package_module, "ROOT_DIR", tmp_path)
    archive_path = data_dir / "seed.zip"
    sha_path = data_dir / "seed.sha256"
    archive_path.write_bytes(b"old-zip")
    sha_path.write_text("OLD-SHA  seed.zip\n", encoding="utf-8")
    real_replace = Path.replace

    def fail_publish_and_rollback(source, target):
        source_path = Path(source)
        target_path = Path(target)
        if source_path.name == "seed.zip.tmp" and target_path == archive_path:
            raise OSError("injected ZIP publish failure")
        if source_path.name.endswith(".rollback"):
            raise OSError("injected rollback failure")
        return real_replace(source, target)

    monkeypatch.setattr(Path, "replace", fail_publish_and_rollback)

    with pytest.raises(RuntimeError, match="rollback was incomplete"):
        package_module.package_seed_cache(archive_path, sha_path, seed_dir)

    assert (data_dir / "seed.zip.rollback").read_bytes() == b"old-zip"
    assert (data_dir / "seed.sha256.rollback").read_text(encoding="utf-8") == "OLD-SHA  seed.zip\n"


def test_package_retry_refuses_to_overwrite_retained_recovery_backups(tmp_path, monkeypatch):
    seed_dir, data_dir, _generation_id, _expected_generation_entries = _write_package_inputs(tmp_path)
    monkeypatch.setattr(package_module, "ROOT_DIR", tmp_path)
    archive_path = data_dir / "seed.zip"
    sha_path = data_dir / "seed.sha256"
    archive_path.write_bytes(b"old-zip")
    sha_path.write_text("OLD-SHA  seed.zip\n", encoding="utf-8")
    real_replace = Path.replace

    def fail_publish_and_rollback(source, target):
        source_path = Path(source)
        target_path = Path(target)
        if source_path.name == "seed.zip.tmp" and target_path == archive_path:
            raise OSError("injected ZIP publish failure")
        if source_path.name.endswith(".rollback"):
            raise OSError("injected rollback failure")
        return real_replace(source, target)

    monkeypatch.setattr(Path, "replace", fail_publish_and_rollback)
    with pytest.raises(RuntimeError, match="rollback was incomplete"):
        package_module.package_seed_cache(archive_path, sha_path, seed_dir)

    zip_backup = data_dir / "seed.zip.rollback"
    sha_backup = data_dir / "seed.sha256.rollback"
    original_zip_backup = zip_backup.read_bytes()
    original_sha_backup = sha_backup.read_bytes()
    inconsistent_zip = archive_path.read_bytes()
    inconsistent_sha = sha_path.read_bytes()

    monkeypatch.setattr(Path, "replace", real_replace)
    with pytest.raises(RuntimeError, match="recovery backups already exist"):
        package_module.package_seed_cache(archive_path, sha_path, seed_dir)

    assert zip_backup.read_bytes() == original_zip_backup
    assert sha_backup.read_bytes() == original_sha_backup
    assert archive_path.read_bytes() == inconsistent_zip
    assert sha_path.read_bytes() == inconsistent_sha


def test_package_rejects_unsafe_archive_name(tmp_path):
    source = tmp_path / "source.json"
    source.write_text("{}", encoding="utf-8")
    archive_path = tmp_path / "archive.zip"

    with zipfile.ZipFile(archive_path, "w") as archive:
        with pytest.raises(ValueError, match="unsafe seed archive path"):
            package_module._add_file(archive, source, "../escape.json")


def test_package_rejects_source_outside_allowed_root(tmp_path):
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")

    with zipfile.ZipFile(tmp_path / "archive.zip", "w") as archive:
        with pytest.raises(ValueError, match="escapes its allowed root"):
            package_module._add_file(
                archive,
                outside,
                "safe.json",
                allowed_root=allowed_root,
            )
