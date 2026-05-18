from scripts.plan_cloudflare_recovery import render_recovery_plan, validate_d1_backup


def test_recovery_plan_renders_non_destructive_commands(tmp_path):
    backup = tmp_path / "backup.sql"
    backup.write_text("PRAGMA foreign_keys=OFF;\nCREATE TABLE users(id INTEGER);\n", encoding="utf-8")

    assert validate_d1_backup(backup) == []
    plan = render_recovery_plan(
        d1_backup=backup,
        worker_version="abc123",
        manifest={"counts": {"companies": 1000, "analysis": 1000}},
        bucket="bucket",
        database="db",
        pages_project="pages",
    )

    assert "wrangler rollback abc123" in plan
    assert "wrangler d1 execute db" in plan
    assert "bucket/public/manifest.json" in plan
    assert "run_remote_smoke.py" in plan


def test_recovery_plan_rejects_non_sql_backup(tmp_path):
    backup = tmp_path / "backup.sql"
    backup.write_text("not sql", encoding="utf-8")

    assert validate_d1_backup(backup)
