from __future__ import annotations

import json
import re

from js import Object, Response
from pyodide.ffi import to_js

try:
    import worker_observability as observability
    from worker_support import SECURITY_HEADERS, empty_market_scan, js_to_py, json_response
except ModuleNotFoundError:
    from cloudflare import worker_observability as observability
    from cloudflare.worker_support import SECURITY_HEADERS, empty_market_scan, js_to_py, json_response

"""Legacy v1 ``/api/scan/market`` support.

The v1 endpoint returns the whole market scan summary (multiple megabytes).
Decoding and re-encoding that JSON inside the Python Worker exhausted Cloudflare
resource limits in production, which took the entire Worker down. The seed build
already compacts every row of ``market_scan_summary.json``, so the Worker now
streams the R2 text through and only splices in the runtime ``cacheStatus``.
"""

MAX_SUMMARY_BYTES = 16 * 1024 * 1024
_MARKET_SCAN_CATEGORY_PATTERNS = tuple(
    re.compile(rf'"{category}"\s*:\s*\[') for category in ("entry", "watch", "excluded")
)


def json_text_response(text: str, status=200, headers=None):
    """Return already-serialized JSON text as a response (no re-encoding)."""
    response_headers = {
        "content-type": "application/json; charset=utf-8",
        "cache-control": "no-store",
        **SECURITY_HEADERS,
    }
    if headers:
        response_headers.update(headers)
    return Response.new(
        text,
        to_js({"status": status, "headers": response_headers}, dict_converter=Object.fromEntries),
    )


def splice_market_summary(raw, cache_status: dict, *, max_bytes: int = MAX_SUMMARY_BYTES):
    """Attach ``cacheStatus``/``detailMode`` to a pre-compacted summary without parsing it.

    Returns ``None`` when the raw text is unusable so callers can fall back safely.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if len(text) < 2 or len(text) > max_bytes or not text.startswith("{") or not text.endswith("}"):
        return None
    body = text[:-1].rstrip()
    extra = json.dumps({"detailMode": "summary", "cacheStatus": cache_status}, ensure_ascii=False)[1:-1]
    if body == "{":
        return "{" + extra + "}"
    return body + "," + extra + "}"


def looks_like_market_scan_text(raw) -> bool:
    """Cheap shape check for a last-good v1 market scan payload (no JSON decode)."""
    if not isinstance(raw, str) or not raw.lstrip().startswith("{"):
        return False
    return all(pattern.search(raw) is not None for pattern in _MARKET_SCAN_CATEGORY_PATTERNS)


async def r2_text(api, key: str, dependency_failure_type):
    """Read an R2 object as text without JSON-decoding it inside the Worker."""

    async def get():
        return js_to_py(await api.env.CACHE.get(key))

    obj = await observability.dependency_call(get, dependency_failure_type, "r2_read", True)
    if obj is None:
        return None
    text = await observability.dependency_call(lambda: obj.text(), dependency_failure_type, "r2_read", True)
    return text if isinstance(text, str) else None


def market_summary_response(raw_scan, cache_status, headers=None):
    spliced = splice_market_summary(raw_scan, cache_status)
    if spliced is None:
        scan = empty_market_scan()
        scan["detailMode"] = "summary"
        scan["cacheStatus"] = cache_status
        return json_response(scan, headers=headers)
    return json_text_response(spliced, headers=headers)


__all__ = (
    "json_text_response",
    "looks_like_market_scan_text",
    "market_summary_response",
    "r2_text",
    "splice_market_summary",
)
