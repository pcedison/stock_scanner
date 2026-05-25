from pathlib import Path

import pytest

from scripts.check_pages_frontend import (
    deployment_source_matches,
    expected_cache_busted_assets,
    missing_assets,
    validate_pages_deployment_metadata,
    validate_pages_url,
    validate_project_name,
)


def test_pages_frontend_expected_assets_are_read_from_index(tmp_path: Path):
    index = tmp_path / "index.html"
    index.write_text(
        """
<link rel="stylesheet" href="/styles.css?v=20260519-design-refresh" />
<script src="/dom.js?v=20260525-dom-hardening" defer></script>
<script src="/app.js?v=20260525-dom-hardening" defer></script>
""",
        encoding="utf-8",
    )

    assert expected_cache_busted_assets(index) == [
        "/app.js?v=20260525-dom-hardening",
        "/dom.js?v=20260525-dom-hardening",
        "/styles.css?v=20260519-design-refresh",
    ]


def test_pages_frontend_missing_assets_reports_only_absent_entries():
    expected = ["/dom.js?v=1", "/app.js?v=2"]

    assert missing_assets('<script src="/dom.js?v=1"></script>', expected) == ["/app.js?v=2"]


def test_pages_frontend_url_is_restricted_to_cloudflare_pages():
    assert validate_pages_url("https://stock-scanner-beta.pages.dev/") == "https://stock-scanner-beta.pages.dev/"

    with pytest.raises(RuntimeError, match=r"https://\*\.pages\.dev"):
        validate_pages_url("http://127.0.0.1:8000/")


def test_pages_project_name_rejects_shell_metacharacters():
    assert validate_project_name("stock-scanner-beta") == "stock-scanner-beta"

    with pytest.raises(RuntimeError, match="unsupported characters"):
        validate_project_name("stock-scanner-beta;echo bad")


def test_pages_metadata_accepts_latest_production_branch_and_source_prefix():
    deployments = [
        {"Environment": "Preview", "Branch": "main", "Source": "old"},
        {"Environment": "Production", "Branch": "main", "Source": "382ca9d"},
    ]

    deployment = validate_pages_deployment_metadata(
        deployments,
        expected_branch="main",
        expected_source="382ca9dddd49d8304aa2523c516a620f2e82227d",
    )

    assert deployment["Source"] == "382ca9d"


def test_pages_metadata_rejects_stale_production_branch():
    deployments = [{"Environment": "Production", "Branch": "codex/old", "Source": "382ca9d"}]

    with pytest.raises(RuntimeError, match="expected 'main'"):
        validate_pages_deployment_metadata(deployments, expected_branch="main")


def test_pages_metadata_rejects_stale_production_source():
    deployments = [{"Environment": "Production", "Branch": "main", "Source": "464a949"}]

    with pytest.raises(RuntimeError, match="expected '382ca9d'"):
        validate_pages_deployment_metadata(deployments, expected_source="382ca9d")


def test_pages_metadata_source_match_allows_short_or_full_sha():
    assert deployment_source_matches("382ca9d", "382ca9dddd49d8304aa2523c516a620f2e82227d")
    assert deployment_source_matches("382ca9dddd49d8304aa2523c516a620f2e82227d", "382ca9d")
    assert not deployment_source_matches("464a949", "382ca9dddd49d8304aa2523c516a620f2e82227d")
