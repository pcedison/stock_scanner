CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    display_name TEXT,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS auth_attempts (
    identifier TEXT PRIMARY KEY,
    failure_count INTEGER NOT NULL,
    first_failed_at TEXT NOT NULL,
    last_failed_at TEXT NOT NULL,
    locked_until TEXT
);

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

CREATE TABLE IF NOT EXISTS app_kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS refresh_jobs (
    id TEXT PRIMARY KEY,
    job_type TEXT NOT NULL,
    cache_key TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    queued_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    updated_at TEXT NOT NULL,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_expires_at ON sessions(expires_at);
CREATE INDEX IF NOT EXISTS idx_auth_attempts_locked_until ON auth_attempts(locked_until);
CREATE INDEX IF NOT EXISTS idx_refresh_jobs_status ON refresh_jobs(job_type, status, queued_at);
