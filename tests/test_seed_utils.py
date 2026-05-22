from scripts.seed_utils import find_seed_zip


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
