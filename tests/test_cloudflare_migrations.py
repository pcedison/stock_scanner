from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _normalized_sql(path: Path) -> str:
    return "\n".join(line.rstrip() for line in path.read_text(encoding="utf-8").strip().splitlines())


def test_initial_d1_migration_matches_schema_snapshot():
    schema = _normalized_sql(ROOT / "cloudflare" / "schema.sql")
    migration = _normalized_sql(ROOT / "cloudflare" / "migrations" / "0001_initial_schema.sql")

    assert migration == schema
