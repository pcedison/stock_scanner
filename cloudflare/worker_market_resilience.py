from __future__ import annotations

try:
    import worker_observability as observability
except ModuleNotFoundError:
    from cloudflare import worker_observability as observability

MARKET_SCAN_PATH = "/api/scan/market"
REFRESH_DEPENDENCY_STAGES = frozenset({"d1_read", "d1_write"})


def is_verified_market_scan(scan) -> bool:
    return isinstance(scan, dict) and all(
        isinstance(scan.get(category), list)
        for category in ("entry", "watch", "excluded")
    )


async def ensure_market_refresh(
    ensure_refresh_job,
    manifest,
    scan,
    dependency_failure_type,
    *,
    force: bool,
    request,
    request_id: str | None,
    reason: str,
):
    has_last_good = is_verified_market_scan(scan)
    request_id = request_id or observability.new_request_id()
    started = observability.start_timer()
    try:
        return await ensure_refresh_job(manifest, force=force)
    except dependency_failure_type as exc:
        if not has_last_good or exc.stage not in REFRESH_DEPENDENCY_STAGES:
            raise
        observability.log_worker_failure(
            event="worker_dependency_degraded",
            request_id=request_id,
            request=request,
            path=MARKET_SCAN_PATH,
            stage="refresh_queue",
            error_type=exc.error_type,
            error_code=getattr(exc, "error_code", "UNCLASSIFIED"),
            status=200,
            duration_ms=observability.duration_ms(started),
        )
        return {
            "status": "unavailable",
            "reason": reason,
            "retryable": exc.retryable,
            "requestId": request_id,
        }


def add_unavailable_cache_metadata(cache_status: dict, refresh_status: dict) -> dict:
    if refresh_status.get("status") == "unavailable":
        cache_status["retryable"] = refresh_status["retryable"]
        cache_status["requestId"] = refresh_status["requestId"]
    return cache_status


async def market_refresh_cache_status(
    *,
    api,
    request,
    manifest,
    scan,
    force: bool,
    dependency_failure_type,
) -> dict:
    refresh_status = await ensure_market_refresh(
        api.ensure_refresh_job,
        manifest,
        scan,
        dependency_failure_type,
        force=force,
        request=request,
        request_id=getattr(api, "_request_id", None),
        reason=api.cache_policy()["reason"],
    )
    cache_status = api.cache_status_from_manifest(manifest, refresh_status)
    return add_unavailable_cache_metadata(cache_status, refresh_status)


__all__ = (
    "add_unavailable_cache_metadata",
    "ensure_market_refresh",
    "is_verified_market_scan",
    "market_refresh_cache_status",
)
