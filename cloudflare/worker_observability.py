from __future__ import annotations

import json
import secrets
import time

SAFE_ERROR_DETAIL = "伺服器暫時無法處理請求，請稍後再試。"
RETRYABLE_D1_READ_CODES = frozenset(
    {"NETWORK_LOST", "RESET", "TRANSIENT_REMOTE_NODE"}
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


async def dependency_call(operation, exception_type, stage: str, retryable):
    failure = None
    try:
        return await operation()
    except exception_type:
        raise
    except Exception as cause:
        can_retry = retryable(cause) if callable(retryable) else retryable
        failure = wrap_dependency_failure(exception_type, stage, bool(can_retry), cause)
    raise failure


def cors_allowed_origins(
    configured,
    development_origins,
    *,
    production,
    allow_local,
    allow_insecure,
    is_local_origin,
    is_https_origin,
):
    allowed = list(dict.fromkeys(configured))
    if not production:
        allowed.extend(origin for origin in development_origins if origin not in allowed)
    if production and not allow_local:
        allowed = [origin for origin in allowed if not is_local_origin(origin)]
    if production and not allow_insecure:
        allowed = [origin for origin in allowed if is_https_origin(origin)]
    return tuple(allowed)


def cors_headers(allowed_origins, request_origin, *, production, local_request_origin, csrf_header_name):
    origin = allowed_origins[0] if allowed_origins else "null"
    if request_origin in allowed_origins or (not production and local_request_origin):
        origin = request_origin
    return {
        "access-control-allow-origin": origin,
        "access-control-allow-methods": "GET,POST,PUT,DELETE,OPTIONS",
        "access-control-allow-headers": f"content-type,{csrf_header_name},idempotency-key",
        "access-control-allow-credentials": "true",
        "vary": "Origin",
    }


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


def log_worker_failure(
    *, request_id, request, path, stage, error_type, error_code, status, duration_ms, event="worker_request_failed"
):
    print(json.dumps({
        "event": str(event)[:80],
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
    "cors_allowed_origins",
    "cors_headers",
    "d1_read_retryable",
    "dependency_call",
    "dependency_error_code",
    "duration_ms",
    "failure_response",
    "log_worker_failure",
    "new_request_id",
    "set_response_header",
    "start_timer",
    "wrap_dependency_failure",
)
