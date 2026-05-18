from scripts.check_operational_readiness import valid_health_url, validate_external_environment, validate_local_readiness


def test_local_operational_readiness_accepts_current_guardrails():
    assert validate_local_readiness() == []


def test_health_url_validation_requires_https_health_path():
    assert valid_health_url("https://example.com/api/health") is True
    assert valid_health_url("http://example.com/api/health") is False
    assert valid_health_url("https://example.com/health") is False


def test_external_environment_validation_reports_missing_values(monkeypatch):
    monkeypatch.delenv("CF_WORKER_HEALTH_URL", raising=False)
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)

    problems = validate_external_environment(require_external=True)

    assert any("CF_WORKER_HEALTH_URL" in problem for problem in problems)
    assert any("CLOUDFLARE_ACCOUNT_ID" in problem for problem in problems)
    assert any("CLOUDFLARE_API_TOKEN" in problem for problem in problems)
