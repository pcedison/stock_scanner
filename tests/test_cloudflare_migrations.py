from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _normalized_sql(path: Path) -> str:
    return "\n".join(line.rstrip() for line in path.read_text(encoding="utf-8").strip().splitlines())


def test_initial_d1_migration_matches_schema_snapshot():
    schema = "\n".join(
        line
        for line in _normalized_sql(ROOT / "cloudflare" / "schema.sql").splitlines()
        if "owner_run_id" not in line and "idx_refresh_jobs_owner_run" not in line
    )
    migration = _normalized_sql(ROOT / "cloudflare" / "migrations" / "0001_initial_schema.sql")

    assert migration == schema


def test_d1_user_children_delete_cascade():
    schema = _normalized_sql(ROOT / "cloudflare" / "schema.sql")

    assert schema.count("FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE") == 2


def test_refresh_jobs_owner_run_id_is_in_schema_and_migration():
    schema = _normalized_sql(ROOT / "cloudflare" / "schema.sql")
    migration = _normalized_sql(ROOT / "cloudflare" / "migrations" / "0002_refresh_jobs_owner_run_id.sql")

    assert "owner_run_id TEXT" in schema
    assert "idx_refresh_jobs_owner_run" in schema
    assert "ALTER TABLE refresh_jobs ADD COLUMN owner_run_id TEXT;" in migration
    assert "idx_refresh_jobs_owner_run" in migration
