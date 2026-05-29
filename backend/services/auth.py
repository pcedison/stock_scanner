from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from backend.models.holding import Holding

ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = ROOT_DIR / "data" / "app.sqlite3"
PASSWORD_ALGORITHM = "pbkdf2_sha256"
PASSWORD_ITERATIONS = 210_000
USERNAME_PATTERN = re.compile(r"^[^\s<>\"'`;]{3,80}$")
AUTH_FAILURE_LIMIT = 5
AUTH_FAILURE_WINDOW_SECONDS = 15 * 60
AUTH_LOCK_SECONDS = 15 * 60
SESSION_CLEANUP_INTERVAL_SECONDS = 15 * 60
MAX_SESSIONS_PER_USER = 10


def _runtime_environment() -> str:
    return os.getenv("APP_ENV", os.getenv("ENVIRONMENT", "development")).strip().lower() or "development"


def super_user_username() -> str:
    configured = os.getenv("SUPER_USER_USERNAME", "").strip().lower()
    if configured:
        return configured
    return ""


class AuthRateLimitError(Exception):
    def __init__(self, retry_after_seconds: int):
        self.retry_after_seconds = retry_after_seconds
        super().__init__("登入嘗試過多，請稍後再試。")


@dataclass(frozen=True)
class AuthUser:
    id: int
    username: str
    display_name: str | None = None

    def public_dict(self) -> dict:
        super_user = super_user_username()
        return {
            "id": self.id,
            "username": self.username,
            "displayName": self.display_name or self.username,
            "isSuperUser": bool(super_user and self.username.lower() == super_user),
        }


class AuthService:
    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self._local = threading.local()
        self._last_session_cleanup_at: datetime | None = None
        self._cleanup_lock = threading.Lock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn"):
            conn = sqlite3.connect(str(self.db_path), timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 5000")
            self._local.conn = conn
        return self._local.conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            delattr(self._local, "conn")

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    display_name TEXT,
                    password_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_sessions_expires_at ON sessions(expires_at);
                CREATE INDEX IF NOT EXISTS idx_sessions_token_hash ON sessions(token_hash);
                CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id);

                CREATE TABLE IF NOT EXISTS auth_attempts (
                    identifier TEXT PRIMARY KEY,
                    failure_count INTEGER NOT NULL,
                    first_failed_at TEXT NOT NULL,
                    last_failed_at TEXT NOT NULL,
                    locked_until TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_auth_attempts_locked_until ON auth_attempts(locked_until);

                CREATE TABLE IF NOT EXISTS holdings (
                    user_id INTEGER NOT NULL,
                    stock_code TEXT NOT NULL,
                    name TEXT,
                    shares INTEGER NOT NULL DEFAULT 0,
                    average_cost REAL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, stock_code),
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                );
                """
            )

    @staticmethod
    def normalize_username(username: str) -> str:
        normalized = username.strip().lower()
        if not USERNAME_PATTERN.fullmatch(normalized):
            raise ValueError("帳號需為 3-80 字元，且不可包含空白或 < > \" ' ` ;")
        return normalized

    @staticmethod
    def validate_password(password: str) -> None:
        if len(password) < 8:
            raise ValueError("密碼至少需要 8 個字元")
        if len(password) > 128:
            raise ValueError("密碼長度不能超過 128 個字元")

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    @staticmethod
    def _parse_time(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _auth_attempt_identifier(username: str, source: str | None) -> str:
        normalized_username = (username or "").strip().lower()
        normalized_source = (source or "unknown").split(",", 1)[0].strip().lower() or "unknown"
        return hashlib.sha256(f"{normalized_source}|{normalized_username}".encode()).hexdigest()

    @staticmethod
    def _hash_password(password: str, salt: bytes | None = None) -> str:
        salt = salt or secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt,
            PASSWORD_ITERATIONS,
        )
        return f"{PASSWORD_ALGORITHM}${PASSWORD_ITERATIONS}${salt.hex()}${digest.hex()}"

    @staticmethod
    def _verify_password(password: str, stored_hash: str) -> bool:
        try:
            algorithm, iterations, salt_hex, digest_hex = stored_hash.split("$", 3)
            if algorithm != PASSWORD_ALGORITHM:
                return False
            digest = hashlib.pbkdf2_hmac(
                "sha256",
                password.encode("utf-8"),
                bytes.fromhex(salt_hex),
                int(iterations),
            )
            return hmac.compare_digest(digest.hex(), digest_hex)
        except (ValueError, TypeError):
            return False

    @staticmethod
    def _row_to_user(row: sqlite3.Row | None) -> AuthUser | None:
        if row is None:
            return None
        return AuthUser(id=int(row["id"]), username=row["username"], display_name=row["display_name"])

    def create_user(self, username: str, password: str, display_name: str | None = None) -> AuthUser:
        normalized_username = self.normalize_username(username)
        self.validate_password(password)
        clean_display_name = (display_name or "").strip()[:80] or None
        now = self._now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO users (username, display_name, password_hash, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (normalized_username, clean_display_name, self._hash_password(password), now),
                )
                user_id = int(cursor.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ValueError("帳號已存在") from exc
        return AuthUser(id=user_id, username=normalized_username, display_name=clean_display_name)

    def authenticate(self, username: str, password: str) -> AuthUser | None:
        try:
            normalized_username = self.normalize_username(username)
        except ValueError:
            return None
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id, username, display_name, password_hash FROM users WHERE username = ?",
                (normalized_username,),
            ).fetchone()
        if row is None or not self._verify_password(password, row["password_hash"]):
            return None
        return self._row_to_user(row)

    def assert_auth_allowed(self, username: str, source: str | None) -> None:
        identifier = self._auth_attempt_identifier(username, source)
        now = datetime.now(UTC)
        stale_before = (now - timedelta(seconds=AUTH_FAILURE_WINDOW_SECONDS)).isoformat()
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM auth_attempts WHERE locked_until IS NULL AND first_failed_at <= ?",
                (stale_before,),
            )
            row = connection.execute(
                "SELECT locked_until FROM auth_attempts WHERE identifier = ?",
                (identifier,),
            ).fetchone()
            locked_until = self._parse_time(row["locked_until"]) if row else None
            if locked_until and locked_until > now:
                retry_after = max(1, int((locked_until - now).total_seconds()))
                raise AuthRateLimitError(retry_after)
            if locked_until:
                connection.execute("DELETE FROM auth_attempts WHERE identifier = ?", (identifier,))

    def record_auth_failure(self, username: str, source: str | None) -> None:
        identifier = self._auth_attempt_identifier(username, source)
        now = datetime.now(UTC)
        now_text = now.isoformat()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT failure_count, first_failed_at FROM auth_attempts WHERE identifier = ?",
                (identifier,),
            ).fetchone()
            first_failed_at = self._parse_time(row["first_failed_at"]) if row else None
            if not first_failed_at or first_failed_at <= now - timedelta(seconds=AUTH_FAILURE_WINDOW_SECONDS):
                failure_count = 1
                first_failed_at = now
            else:
                failure_count = int(row["failure_count"]) + 1
            locked_until = (now + timedelta(seconds=AUTH_LOCK_SECONDS)).isoformat() if failure_count >= AUTH_FAILURE_LIMIT else None
            connection.execute(
                """
                INSERT INTO auth_attempts (identifier, failure_count, first_failed_at, last_failed_at, locked_until)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(identifier) DO UPDATE SET
                    failure_count = excluded.failure_count,
                    first_failed_at = excluded.first_failed_at,
                    last_failed_at = excluded.last_failed_at,
                    locked_until = excluded.locked_until
                """,
                (identifier, failure_count, first_failed_at.isoformat(), now_text, locked_until),
            )

    def clear_auth_failures(self, username: str, source: str | None) -> None:
        identifier = self._auth_attempt_identifier(username, source)
        with self._connect() as connection:
            connection.execute("DELETE FROM auth_attempts WHERE identifier = ?", (identifier,))

    def _should_cleanup_sessions(self, now: datetime) -> bool:
        with self._cleanup_lock:
            last_cleanup = self._last_session_cleanup_at
            if last_cleanup and last_cleanup > now - timedelta(seconds=SESSION_CLEANUP_INTERVAL_SECONDS):
                return False
            self._last_session_cleanup_at = now
            return True

    def create_session(self, user_id: int, days: int = 30) -> str:
        token = secrets.token_urlsafe(32)
        now = datetime.now(UTC)
        expires_at = now + timedelta(days=days)
        with self._connect() as connection:
            connection.execute(
                """
                DELETE FROM sessions WHERE user_id = ? AND token_hash NOT IN (
                    SELECT token_hash FROM sessions WHERE user_id = ?
                    ORDER BY created_at DESC LIMIT ?
                )
                """,
                (user_id, user_id, MAX_SESSIONS_PER_USER - 1),
            )
            connection.execute(
                """
                INSERT INTO sessions (user_id, token_hash, created_at, expires_at)
                VALUES (?, ?, ?, ?)
                """,
                (user_id, self._token_hash(token), now.isoformat(), expires_at.isoformat()),
            )
        return token

    def get_user_by_session(self, token: str | None) -> AuthUser | None:
        if not token:
            return None
        now_dt = datetime.now(UTC)
        now = now_dt.isoformat()
        with self._connect() as connection:
            if self._should_cleanup_sessions(now_dt):
                connection.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
            row = connection.execute(
                """
                SELECT users.id, users.username, users.display_name
                FROM sessions
                JOIN users ON users.id = sessions.user_id
                WHERE sessions.token_hash = ? AND sessions.expires_at > ?
                """,
                (self._token_hash(token), now),
            ).fetchone()
        return self._row_to_user(row)

    def delete_session(self, token: str | None) -> None:
        if not token:
            return
        with self._connect() as connection:
            connection.execute("DELETE FROM sessions WHERE token_hash = ?", (self._token_hash(token),))

    def list_holdings(self, user_id: int) -> list[Holding]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT stock_code, name, shares, average_cost
                FROM holdings
                WHERE user_id = ?
                ORDER BY stock_code
                """,
                (user_id,),
            ).fetchall()
        return [
            Holding(
                stockCode=row["stock_code"],
                name=row["name"],
                shares=int(row["shares"]),
                averageCost=row["average_cost"],
            )
            for row in rows
        ]

    def replace_holdings(self, user_id: int, holdings: list[Holding]) -> list[Holding]:
        now = self._now()
        with self._connect() as connection:
            connection.execute("DELETE FROM holdings WHERE user_id = ?", (user_id,))
            for holding in holdings:
                connection.execute(
                    """
                    INSERT INTO holdings (user_id, stock_code, name, shares, average_cost, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        holding.stockCode,
                        holding.name,
                        holding.shares,
                        holding.averageCost,
                        now,
                        now,
                    ),
                )
        return self.list_holdings(user_id)

    def upsert_holding(self, user_id: int, holding: Holding) -> list[Holding]:
        now = self._now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO holdings (user_id, stock_code, name, shares, average_cost, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, stock_code) DO UPDATE SET
                    name = excluded.name,
                    shares = excluded.shares,
                    average_cost = excluded.average_cost,
                    updated_at = excluded.updated_at
                """,
                (
                    user_id,
                    holding.stockCode,
                    holding.name,
                    holding.shares,
                    holding.averageCost,
                    now,
                    now,
                ),
            )
        return self.list_holdings(user_id)

    def delete_holding(self, user_id: int, stock_code: str) -> list[Holding]:
        with self._connect() as connection:
            connection.execute("DELETE FROM holdings WHERE user_id = ? AND stock_code = ?", (user_id, stock_code))
        return self.list_holdings(user_id)

    @staticmethod
    def is_super_user(user: AuthUser | None) -> bool:
        super_user = super_user_username()
        return bool(user and super_user and user.username.lower() == super_user)

    def list_users(self) -> list[dict]:
        now = self._now()
        super_user = super_user_username()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    users.id,
                    users.username,
                    users.display_name,
                    users.created_at,
                    COUNT(DISTINCT holdings.stock_code) AS holdings_count,
                    COUNT(DISTINCT sessions.token_hash) AS active_session_count
                FROM users
                LEFT JOIN holdings ON holdings.user_id = users.id
                LEFT JOIN sessions ON sessions.user_id = users.id AND sessions.expires_at > ?
                GROUP BY users.id, users.username, users.display_name, users.created_at
                ORDER BY
                    CASE WHEN users.username = ? THEN 0 ELSE 1 END,
                    users.created_at DESC,
                    users.username ASC
                """,
                (now, super_user),
            ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "username": row["username"],
                "displayName": row["display_name"] or row["username"],
                "createdAt": row["created_at"],
                "holdingsCount": int(row["holdings_count"] or 0),
                "activeSessionCount": int(row["active_session_count"] or 0),
                "isSuperUser": bool(super_user and row["username"].lower() == super_user),
                "canDelete": not (super_user and row["username"].lower() == super_user),
            }
            for row in rows
        ]

    def delete_user(self, user_id: int) -> bool:
        super_user = super_user_username()
        with self._connect() as connection:
            row = connection.execute("SELECT id, username FROM users WHERE id = ?", (user_id,)).fetchone()
            if row is None:
                return False
            if super_user and row["username"].lower() == super_user:
                raise ValueError("super user cannot be deleted")
            connection.execute("DELETE FROM users WHERE id = ?", (user_id,))
        return True
