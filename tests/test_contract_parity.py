"""Guard that the FastAPI backend and the Cloudflare Worker resolve the same
shared contract constants (cloudflare/contract.py). Diverging values here are
security bugs (e.g. password hashes unverifiable across runtimes, or different
login throttling), so this test fails fast if either side re-introduces a local
override instead of importing the single source of truth.
"""

from backend import main
from backend.services import auth
from cloudflare import contract
from tests.test_cloudflare_worker import load_worker_module

AUTH_CONSTANTS = (
    "PASSWORD_ALGORITHM",
    "PASSWORD_ITERATIONS",
    "USERNAME_PATTERN",
    "AUTH_FAILURE_LIMIT",
    "AUTH_FAILURE_WINDOW_SECONDS",
    "AUTH_LOCK_SECONDS",
)
CSRF_CONSTANTS = (
    "CSRF_HEADER_NAME",
    "CSRF_HEADER_VALUE",
    "UNSAFE_API_METHODS",
)


def _comparable(value):
    # USERNAME_PATTERN is a compiled regex; compare by its source pattern.
    return value.pattern if hasattr(value, "pattern") else value


def test_backend_auth_uses_shared_contract_constants():
    for name in AUTH_CONSTANTS:
        assert _comparable(getattr(auth, name)) == _comparable(getattr(contract, name)), name


def test_backend_main_uses_shared_contract_constants():
    for name in CSRF_CONSTANTS:
        assert getattr(main, name) == getattr(contract, name), name


def test_worker_runtime_matches_shared_contract_constants(monkeypatch):
    worker = load_worker_module(monkeypatch)
    for name in AUTH_CONSTANTS + CSRF_CONSTANTS:
        assert _comparable(getattr(worker, name)) == _comparable(getattr(contract, name)), name
