from pathlib import Path

from scripts.seed_utils import find_seed_zip, main, resolve_seed_zip


def test_find_seed_zip_selects_newest_dated_artifact(tmp_path):
    older = tmp_path / "official_cache_seed_2026-05-14.zip"
    newer = tmp_path / "official_cache_seed_2026-05-21.zip"
    sentinel = tmp_path / "official_cache_seed_latest.zip"
    older.write_bytes(b"older")
    newer.write_bytes(b"newer")
    sentinel.write_bytes(b"sentinel")

    assert find_seed_zip(tmp_path) == newer


def test_find_seed_zip_falls_back_to_latest_sentinel_path(tmp_path):
    assert find_seed_zip(tmp_path) == tmp_path / "official_cache_seed_latest.zip"


def test_resolve_seed_zip_raises_when_no_dated_artifact(tmp_path):
    (tmp_path / "official_cache_seed_latest.zip").write_bytes(b"sentinel")

    try:
        resolve_seed_zip(tmp_path)
    except FileNotFoundError as exc:
        assert "No dated seed zip found" in str(exc)
    else:
        raise AssertionError("resolve_seed_zip should fail without a dated seed zip")


def test_seed_utils_cli_prints_newest_dated_artifact(tmp_path, capsys):
    older = tmp_path / "official_cache_seed_2026-05-14.zip"
    newer = tmp_path / "official_cache_seed_2026-05-21.zip"
    older.write_bytes(b"older")
    newer.write_bytes(b"newer")

    assert main([str(tmp_path), "--print"]) == 0
    assert capsys.readouterr().out.strip() == str(newer)


def test_seed_utils_cli_fails_when_no_dated_artifact(tmp_path, capsys):
    assert main([str(tmp_path), "--print"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "No dated seed zip found" in captured.err


def test_gitignore_requires_intentional_seed_artifact_updates():
    text = Path(".gitignore").read_text(encoding="utf-8")

    assert "data/official_cache_seed_*.zip" in text
    assert "data/official_cache_seed_*.sha256" in text
    assert "!data/official_cache_seed_2026-05-14.zip" in text
    assert "!data/official_cache_seed_2026-05-14.sha256" in text
