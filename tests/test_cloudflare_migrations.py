import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _normalized_sql(path: Path) -> str:
    return "\n".join(line.rstrip() for line in path.read_text(encoding="utf-8").strip().splitlines())


def _database_structure(database: sqlite3.Connection) -> dict:
    tables = [
        row[0]
        for row in database.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    structure = {}
    for table in tables:
        indexes = {}
        for _seq, name, unique, origin, partial in database.execute(f'PRAGMA index_list("{table}")'):
            indexes[name] = {
                "unique": unique,
                "origin": origin,
                "partial": partial,
                "columns": [tuple(row[2:]) for row in database.execute(f'PRAGMA index_xinfo("{name}")')],
            }
        structure[table] = {
            "columns": {
                row[1]: tuple(row[2:]) for row in database.execute(f'PRAGMA table_info("{table}")')
            },
            "foreign_keys": sorted(
                tuple(row[2:]) for row in database.execute(f'PRAGMA foreign_key_list("{table}")')
            ),
            "indexes": indexes,
        }
    return structure


def test_refresh_job_migrations_match_current_schema_structure():
    schema_database = sqlite3.connect(":memory:")
    schema_database.executescript((ROOT / "cloudflare" / "schema.sql").read_text(encoding="utf-8"))
    migration_database = sqlite3.connect(":memory:")
    for migration in sorted((ROOT / "cloudflare" / "migrations").glob("*.sql")):
        migration_database.executescript(migration.read_text(encoding="utf-8"))

    assert _database_structure(migration_database) == _database_structure(schema_database)


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


def test_refresh_jobs_idempotency_migration_executes_with_sqlite_semantics():
    database = sqlite3.connect(":memory:")
    database.executescript((ROOT / "cloudflare" / "migrations" / "0001_initial_schema.sql").read_text(encoding="utf-8"))
    database.executescript(
        (ROOT / "cloudflare" / "migrations" / "0002_refresh_jobs_owner_run_id.sql").read_text(encoding="utf-8")
    )
    rows = (
        ("old-active", "market_scan", "cache-a", "queued", "manual", "2026-07-13T00:00:00+00:00"),
        ("new-active", "market_scan", "cache-a", "running", "manual", "2026-07-13T00:01:00+00:00"),
        ("other-active", "market_scan", "cache-b", "queued", "manual", "2026-07-13T00:02:00+00:00"),
        ("legacy-success", "market_scan", "cache-a", "success", "manual", "2026-07-12T00:00:00+00:00"),
    )
    database.executemany(
        """
        INSERT INTO refresh_jobs (id, job_type, cache_key, status, reason, queued_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [(*row, row[-1]) for row in rows],
    )

    database.executescript(
        (ROOT / "cloudflare" / "migrations" / "0003_refresh_jobs_idempotency.sql").read_text(encoding="utf-8")
    )

    columns = {row[1]: row for row in database.execute("PRAGMA table_info(refresh_jobs)")}
    indexes = {
        row[0]: row[1]
        for row in database.execute(
            "SELECT name, sql FROM sqlite_schema WHERE type = 'index' AND tbl_name = 'refresh_jobs'"
        )
        if row[1]
    }
    superseded = database.execute(
        "SELECT status, error, finished_at FROM refresh_jobs WHERE id = 'old-active'"
    ).fetchone()
    active_ids = {
        row[0]
        for row in database.execute(
            "SELECT id FROM refresh_jobs WHERE status IN ('queued', 'running') ORDER BY id"
        )
    }

    assert columns["idempotency_key"][3] == 0
    assert active_ids == {"new-active", "other-active"}
    assert superseded[0:2] == ("failed", "superseded before active uniqueness migration")
    assert superseded[2]
    assert "WHERE idempotency_key IS NOT NULL" in indexes["ux_refresh_jobs_idempotency_key"]
    assert "WHERE status IN ('queued', 'running')" in indexes["ux_refresh_jobs_active_cache"]

    database.execute(
        """
        INSERT INTO refresh_jobs (id, job_type, cache_key, idempotency_key, status, reason, queued_at, updated_at)
        VALUES ('legacy-null-2', 'market_scan', 'cache-a', NULL, 'success', 'manual', '2026-07-13', '2026-07-13')
        """
    )
    database.execute(
        """
        INSERT INTO refresh_jobs (id, job_type, cache_key, idempotency_key, status, reason, queued_at, updated_at)
        VALUES ('with-key', 'market_scan', 'cache-c', 'hashed-key', 'success', 'manual', '2026-07-13', '2026-07-13')
        """
    )
    with pytest.raises(sqlite3.IntegrityError):
        database.execute(
            """
            INSERT INTO refresh_jobs (id, job_type, cache_key, idempotency_key, status, reason, queued_at, updated_at)
            VALUES ('duplicate-key', 'market_scan', 'cache-d', 'hashed-key', 'success', 'manual', '2026-07-13', '2026-07-13')
            """
        )
    with pytest.raises(sqlite3.IntegrityError):
        database.execute(
            """
            INSERT INTO refresh_jobs (id, job_type, cache_key, status, reason, queued_at, updated_at)
            VALUES ('duplicate-active', 'market_scan', 'cache-a', 'queued', 'manual', '2026-07-13', '2026-07-13')
            """
        )


def test_refresh_jobs_idempotency_schema_snapshot_declares_nullable_column_and_unique_indexes():
    schema = _normalized_sql(ROOT / "cloudflare" / "schema.sql")

    assert "idempotency_key TEXT" in schema
    assert "ux_refresh_jobs_idempotency_key" in schema
    assert "ux_refresh_jobs_active_cache" in schema


def test_refresh_jobs_dispatch_state_migration_preserves_legacy_rows_and_insert_defaults():
    database = sqlite3.connect(":memory:")
    for name in (
        "0001_initial_schema.sql",
        "0002_refresh_jobs_owner_run_id.sql",
        "0003_refresh_jobs_idempotency.sql",
    ):
        database.executescript((ROOT / "cloudflare" / "migrations" / name).read_text(encoding="utf-8"))
    database.execute(
        """
        INSERT INTO refresh_jobs
        (id, job_type, cache_key, status, reason, queued_at, updated_at, owner_run_id)
        VALUES ('legacy-row', 'market_scan', 'legacy-cache', 'success', 'manual', '2026-07-12', '2026-07-12', '999')
        """
    )
    database.execute(
        """
        INSERT INTO refresh_jobs
        (id, job_type, cache_key, status, reason, queued_at, updated_at)
        VALUES ('legacy-unclaimed', 'market_scan', 'unclaimed-cache', 'failed', 'manual', '2026-07-12', '2026-07-12')
        """
    )
    database.execute(
        """
        INSERT INTO refresh_jobs
        (id, job_type, cache_key, status, reason, queued_at, updated_at)
        VALUES ('legacy-queued', 'market_scan', 'queued-cache', 'queued', 'manual', '2026-07-13', '2026-07-13')
        """
    )

    migration = ROOT / "cloudflare" / "migrations" / "0004_refresh_jobs_dispatch_state.sql"
    database.executescript(migration.read_text(encoding="utf-8"))

    columns = {row[1]: row for row in database.execute("PRAGMA table_info(refresh_jobs)")}
    indexes = {
        row[0]: row[1]
        for row in database.execute(
            "SELECT name, sql FROM sqlite_schema WHERE type = 'index' AND tbl_name = 'refresh_jobs'"
        )
        if row[1]
    }
    legacy_terminal = database.execute(
        """
        SELECT dispatch_status, dispatch_attempts, dispatched_at, dispatch_error_code
        FROM refresh_jobs WHERE id = 'legacy-row'
        """
    ).fetchone()
    legacy_queued = database.execute(
        "SELECT dispatch_status, dispatch_attempts FROM refresh_jobs WHERE id = 'legacy-queued'"
    ).fetchone()
    legacy_unclaimed = database.execute(
        "SELECT dispatch_status, dispatch_attempts FROM refresh_jobs WHERE id = 'legacy-unclaimed'"
    ).fetchone()
    database.execute(
        """
        INSERT INTO refresh_jobs
        (id, job_type, cache_key, status, reason, queued_at, updated_at)
        VALUES ('old-insert-shape', 'market_scan', 'new-cache', 'queued', 'manual', '2026-07-13', '2026-07-13')
        """
    )
    inserted = database.execute(
        "SELECT dispatch_status, dispatch_attempts FROM refresh_jobs WHERE id = 'old-insert-shape'"
    ).fetchone()

    assert columns["dispatch_status"][2:5] == ("TEXT", 1, "'pending'")
    assert columns["dispatch_attempts"][2:5] == ("INTEGER", 1, "0")
    assert columns["dispatched_at"][2:5] == ("TEXT", 0, None)
    assert columns["dispatch_error_code"][2:5] == ("TEXT", 0, None)
    assert legacy_terminal == ("workflow_claimed", 0, None, None)
    assert legacy_queued == ("pending", 0)
    assert legacy_unclaimed == ("pending", 0)
    assert inserted == ("pending", 0)
    assert "idx_refresh_jobs_dispatch" in indexes
    assert "ON refresh_jobs(job_type, status, dispatch_status, queued_at)" in indexes[
        "idx_refresh_jobs_dispatch"
    ]


def test_refresh_jobs_dispatch_state_schema_snapshot_declares_matching_fields_and_index():
    schema = _normalized_sql(ROOT / "cloudflare" / "schema.sql")
    migration = _normalized_sql(
        ROOT / "cloudflare" / "migrations" / "0004_refresh_jobs_dispatch_state.sql"
    )

    for field in ("dispatch_status", "dispatch_attempts", "dispatched_at", "dispatch_error_code"):
        assert field in schema
        assert field in migration
    assert "idx_refresh_jobs_dispatch" in schema
    assert "idx_refresh_jobs_dispatch" in migration
