from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

from backend.models.settings import ScannerSettings
from backend.services.scan_cache import ScanCacheService, scan_cache_key


def test_scan_cache_returns_cached_payload_without_rebuilding(tmp_path):
    service = ScanCacheService(tmp_path / "scan.json", tmp_path / "jobs.json")
    settings = ScannerSettings(use_mock_data=False)
    calls = {"count": 0}

    def build():
        calls["count"] += 1
        return {"generatedAt": "2026-05-14T00:00:00+00:00", "entry": [{"stockCode": "2330"}], "watch": [], "excluded": []}

    first = service.get_or_refresh(settings, build, refresh_mode="auto")
    second = service.get_or_refresh(settings, build, refresh_mode="cache_only")

    assert calls["count"] == 1
    assert first["cacheStatus"]["cacheHit"] is False
    assert second["cacheStatus"]["cacheHit"] is True
    assert second["entry"][0]["stockCode"] == "2330"


def test_stale_scan_cache_queues_single_background_refresh(tmp_path):
    service = ScanCacheService(tmp_path / "scan.json", tmp_path / "jobs.json")
    settings = ScannerSettings(use_mock_data=False)
    key = scan_cache_key(settings)
    service.store(
        key,
        settings,
        {"generatedAt": "2026-05-14T00:00:00+00:00", "entry": [], "watch": [], "excluded": []},
        {"strategy": "stale_while_revalidate", "reason": "test", "minIntervalSeconds": 1},
    )
    cache_path = tmp_path / "scan.json"
    cached = json.loads(cache_path.read_text(encoding="utf-8"))
    cached["items"][key]["storedAt"] = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    cache_path.write_text(json.dumps(cached), encoding="utf-8")
    calls = {"count": 0}

    def refresh():
        calls["count"] += 1
        time.sleep(0.2)
        return {"generatedAt": "2026-05-14T01:00:00+00:00", "entry": [{"stockCode": "2454"}], "watch": [], "excluded": []}

    response = service.get_or_refresh(settings, refresh, build_refresh=refresh, refresh_mode="auto")
    duplicate = service.get_or_refresh(settings, refresh, build_refresh=refresh, refresh_mode="auto")

    assert response["cacheStatus"]["cacheHit"] is True
    assert response["cacheStatus"]["isStale"] is True
    assert response["cacheStatus"]["refreshStatus"] in {"queued", "running"}
    assert duplicate["cacheStatus"]["refreshStatus"] in {"queued", "running"}

    deadline = time.time() + 3
    status = service.status(settings)
    while status["recentJobs"][-1]["status"] != "success" and time.time() < deadline:
        time.sleep(0.05)
        status = service.status(settings)

    assert calls["count"] == 1
    assert status["recentJobs"][-1]["status"] == "success"
