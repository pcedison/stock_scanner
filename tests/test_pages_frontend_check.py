from pathlib import Path

import pytest

from scripts.check_pages_frontend import expected_cache_busted_assets, missing_assets, validate_pages_url


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
