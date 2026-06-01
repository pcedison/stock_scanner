"""Branch coverage for AuthService helpers, lockout cleanup, and holdings."""

from datetime import UTC, datetime, timedelta

import pytest

from backend.models.holding import Holding
from backend.services.auth import (
    AuthRateLimitError,
    AuthService,
    _runtime_environment,
)


def _service(tmp_path):
    return AuthService(tmp_path / "auth.db")


# --- module / static helpers ------------------------------------------------


def test_runtime_environment_defaults_when_blank(monkeypatch):
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.setenv("APP_ENV", "   ")
    assert _runtime_environment() == "development"


def test_validate_password_length_bounds():
    with pytest.raises(ValueError, match="至少需要 8"):
        AuthService.validate_password("short")
    with pytest.raises(ValueError, match="不能超過 128"):
        AuthService.validate_password("x" * 129)


def test_parse_time_returns_none_for_blank_and_bad_values():
    assert AuthService._parse_time(None) is None
    assert AuthService._parse_time("not-a-timestamp") is None
    assert AuthService._parse_time("2026-01-01T00:00:00+00:00") is not None


def test_verify_password_rejects_wrong_algorithm_and_malformed_hash():
    valid = AuthService._hash_password("correct-horse")
    assert AuthService._verify_password("correct-horse", valid) is True
    assert AuthService._verify_password("correct-horse", valid.replace("pbkdf2_sha256", "argon2", 1)) is False
    assert AuthService._verify_password("correct-horse", "garbage-without-delimiters") is False


# --- user lifecycle ---------------------------------------------------------


def test_create_user_rejects_duplicate_username(tmp_path):
    service = _service(tmp_path)
    service.create_user("trader01", "password123")
    with pytest.raises(ValueError, match="帳號已存在"):
        service.create_user("trader01", "password123")


def test_authenticate_returns_none_for_invalid_username(tmp_path):
    service = _service(tmp_path)
    assert service.authenticate("a b", "password123") is None  # normalize_username raises -> None


# --- lockout cleanup --------------------------------------------------------


def test_assert_auth_allowed_clears_expired_lock(tmp_path):
    service = _service(tmp_path)
    identifier = service._auth_attempt_identifier("trader01", "1.2.3.4")
    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    with service._connect() as connection:
        connection.execute(
            "INSERT INTO auth_attempts (identifier, failure_count, first_failed_at, last_failed_at, locked_until) "
            "VALUES (?, ?, ?, ?, ?)",
            (identifier, 5, past, past, past),
        )

    service.assert_auth_allowed("trader01", "1.2.3.4")  # expired lock -> cleared, no raise

    with service._connect() as connection:
        remaining = connection.execute(
            "SELECT 1 FROM auth_attempts WHERE identifier = ?", (identifier,)
        ).fetchone()
    assert remaining is None


def test_assert_auth_allowed_raises_while_locked(tmp_path):
    service = _service(tmp_path)
    identifier = service._auth_attempt_identifier("trader01", "1.2.3.4")
    future = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    now = datetime.now(UTC).isoformat()
    with service._connect() as connection:
        connection.execute(
            "INSERT INTO auth_attempts (identifier, failure_count, first_failed_at, last_failed_at, locked_until) "
            "VALUES (?, ?, ?, ?, ?)",
            (identifier, 5, now, now, future),
        )
    with pytest.raises(AuthRateLimitError):
        service.assert_auth_allowed("trader01", "1.2.3.4")


# --- sessions & holdings ----------------------------------------------------


def test_delete_session_is_noop_without_token(tmp_path):
    service = _service(tmp_path)
    service.delete_session(None)  # returns early, no DB access
    service.delete_session("")


def test_upsert_holding_inserts_then_updates(tmp_path):
    service = _service(tmp_path)
    user = service.create_user("trader01", "password123")

    after_insert = service.upsert_holding(user.id, Holding(stockCode="2330", name="台積電", shares=10, averageCost=900.0))
    assert [(h.stockCode, h.shares) for h in after_insert] == [("2330", 10)]

    after_update = service.upsert_holding(user.id, Holding(stockCode="2330", name="台積電", shares=25, averageCost=950.0))
    assert [(h.stockCode, h.shares) for h in after_update] == [("2330", 25)]  # ON CONFLICT updated in place
