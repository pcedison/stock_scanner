from __future__ import annotations


def health_payload(manifest, manifest_quality, cache_status_from_manifest, cache_policy, utc_now):
    quality = manifest_quality(manifest)
    policy = cache_policy()
    cache_status = cache_status_from_manifest(
        manifest,
        {"status": "not_requested", "reason": policy["reason"]},
    )
    return {
        "status": "ok" if quality["ok"] and not cache_status["isStale"] else "degraded",
        "runtime": "cloudflare-python-worker",
        "time": utc_now(),
        "cache": manifest,
        "cacheStatus": cache_status,
        "cacheQuality": quality,
    }
