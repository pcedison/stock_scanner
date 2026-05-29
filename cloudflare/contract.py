"""Shared API-contract constants for the FastAPI backend and the Cloudflare Worker.

These values MUST stay identical across both runtimes (e.g. diverging
``PASSWORD_ITERATIONS`` would make password hashes unverifiable across runtimes,
and a diverging ``AUTH_FAILURE_LIMIT`` would change rate limiting). To guarantee
that, this is the single source of truth imported by both sides:

- ``backend`` (CPython / FastAPI) imports via ``from cloudflare.contract import ...``
- ``cloudflare/worker_support.py`` (Pyodide Worker) imports it too; wrangler
  bundles this module alongside the Worker.

Pure stdlib only (no ``js`` / ``pyodide`` imports) so it is importable in plain
CPython and safe to bundle into the Python Worker. ``tests/test_contract_parity.py``
asserts both runtimes resolve to these exact values.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

# Password hashing
PASSWORD_ALGORITHM = "pbkdf2_sha256"
PASSWORD_ITERATIONS = 210_000

# Account identity
USERNAME_PATTERN = re.compile(r"^[^\s<>\"'`;]{3,80}$")

# Login throttling
AUTH_FAILURE_LIMIT = 5
AUTH_FAILURE_WINDOW_SECONDS = 15 * 60
AUTH_LOCK_SECONDS = 15 * 60

# CSRF / unsafe-method guard
CSRF_HEADER_NAME = "x-stock-scanner-csrf"
CSRF_HEADER_VALUE = "1"
UNSAFE_API_METHODS = frozenset({"POST", "PUT", "DELETE"})

# CORS origin classification (loopback hosts allowed only outside production)
LOCAL_CORS_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def origin_host(origin: str) -> str:
    return (urlparse(str(origin or "")).hostname or "").lower()


def is_local_cors_origin(origin: str) -> bool:
    return origin_host(origin) in LOCAL_CORS_HOSTS


def is_https_origin(origin: str) -> bool:
    return urlparse(str(origin or "")).scheme.lower() == "https"


__all__ = (
    "PASSWORD_ALGORITHM",
    "PASSWORD_ITERATIONS",
    "USERNAME_PATTERN",
    "AUTH_FAILURE_LIMIT",
    "AUTH_FAILURE_WINDOW_SECONDS",
    "AUTH_LOCK_SECONDS",
    "CSRF_HEADER_NAME",
    "CSRF_HEADER_VALUE",
    "UNSAFE_API_METHODS",
    "LOCAL_CORS_HOSTS",
    "origin_host",
    "is_local_cors_origin",
    "is_https_origin",
)
