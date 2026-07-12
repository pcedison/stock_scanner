from __future__ import annotations

import json
import secrets
import time

SAFE_ERROR_DETAIL = "伺服器暫時無法處理請求，請稍後再試。"
RETRYABLE_D1_READ_CODES = frozenset(
    {"NETWORK_LOST", "RESET", "TRANSIENT_REMOTE_NODE", "OVERLOADED", "TIMEOUT"}
)
_DEPENDENCY_ERROR_PATTERNS = (
    ("network connection lost", "NETWORK_LOST"),
    ("transient issue on remote node", "TRANSIENT_REMOTE_NODE"),
    ("overloaded", "OVERLOADED"),
    ("timeout", "TIMEOUT"),
    ("timed out", "TIMEOUT"),
    ("reset because its code was updated", "RESET"),
    ("storage caused object to be reset", "RESET"),
    ("connection reset", "RESET"),
)


def new_request_id() -> str:
    return secrets.token_hex(12)


def start_timer() -> float:
    return time.perf_counter()


def duration_ms(started: float) -> float:
    return max(0, round((time.perf_counter() - started) * 1000, 2))


def dependency_error_code(cause: Exception) -> str:
    try:
        message = str(cause).lower()
    except Exception:
        return "UNCLASSIFIED"
    return next(
        (code for marker, code in _DEPENDENCY_ERROR_PATTERNS if marker in message),
        "UNCLASSIFIED",
    )


def d1_read_retryable(cause: Exception) -> bool:
    return dependency_error_code(cause) in RETRYABLE_D1_READ_CODES


def wrap_dependency_failure(exception_type, stage: str, retryable: bool, cause: Exception):
    failure = exception_type(stage, retryable, cause)
    failure.error_code = dependency_error_code(cause)
    return failure


def set_response_header(response, key: str, value: str) -> None:
    headers = getattr(response, "headers", None)
    if headers is not None and hasattr(headers, "set"):
        headers.set(key, value)
    elif isinstance(headers, dict):
        headers[key] = value


def add_response_headers(response, headers: dict[str, str], request_id: str) -> None:
    for key, value in headers.items():
        set_response_header(response, key, value)
    set_response_header(response, "x-request-id", request_id)


def log_worker_failure(*, request_id, request, path, stage, error_type, error_code, status, duration_ms):
    print(json.dumps({
        "event": "worker_request_failed",
        "requestId": request_id,
        "method": str(getattr(request, "method", ""))[:12],
        "path": path[:160],
        "stage": stage,
        "status": status,
        "errorType": str(error_type)[:120],
        "errorCode": str(error_code)[:80],
        "durationMs": max(0, round(float(duration_ms), 2)),
    }, ensure_ascii=False, sort_keys=True))


def failure_response(response_factory, request_id, request, path, failure, started, *, dependency, stage=None):
    retryable = bool(dependency and failure.retryable)
    stage = stage or (failure.stage if dependency else "route")
    status = 503 if retryable else 500
    if retryable:
        response_code = "DEPENDENCY_UNAVAILABLE"
    elif dependency:
        response_code = "DEPENDENCY_FAILURE"
    else:
        response_code = "INTERNAL_ERROR"
    error_type = failure.error_type if dependency else type(failure).__name__
    error_code = getattr(failure, "error_code", "UNCLASSIFIED") if dependency else "UNCLASSIFIED"
    log_worker_failure(
        request_id=request_id,
        request=request,
        path=path,
        stage=stage,
        error_type=error_type,
        error_code=error_code,
        status=status,
        duration_ms=duration_ms(started),
    )
    return response_factory(
        SAFE_ERROR_DETAIL,
        status=status,
        code=response_code,
        request_id=request_id,
        retryable=retryable,
        stage=stage,
    )


__all__ = (
    "add_response_headers",
    "d1_read_retryable",
    "dependency_error_code",
    "duration_ms",
    "failure_response",
    "log_worker_failure",
    "new_request_id",
    "set_response_header",
    "start_timer",
    "wrap_dependency_failure",
)
