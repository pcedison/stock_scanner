from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from backend.models.settings import ScannerSettings
from backend.services import scan_cache as scan_cache_module
from backend.services.scan_cache import (
    ScanCacheService,
    _parse_time,
    _public_job,
    scan_cache_key,
)


def test_scan_cache_returns_cached_payload_without_rebuilding(tmp_path):
    service = ScanCacheService(tmp_path / "scan.json", tmp_path / "jobs.json")
    settings = ScannerSettings(use_mock_data=False)
    calls = {"count": 0}

    def build():
        calls["count"] += 1
        return {
            "generatedAt": "2026-05-14T00:00:00+00:00",
            "entry": [{"stockCode": "2330"}],
            "watch": [],
            "excluded": [],
        }

    first = service.get_or_refresh(settings, build, refresh_mode="auto")
    second = service.get_or_refresh(settings, build, refresh_mode="cache_only")

    assert calls["count"] == 1
    assert first["cacheStatus"]["cacheHit"] is False
    assert second["cacheStatus"]["cacheHit"] is True
    assert second["entry"][0]["stockCode"] == "2330"


def test_scan_cache_key_includes_data_context():
    settings = ScannerSettings(use_mock_data=False)

    first = scan_cache_key(settings, {"ruleset": "v1", "data": {"monthly": {"mtimeNs": 1, "size": 10}}})
    second = scan_cache_key(settings, {"ruleset": "v1", "data": {"monthly": {"mtimeNs": 2, "size": 10}}})

    assert first != second


def test_scan_cache_context_change_rebuilds_payload(tmp_path):
    service = ScanCacheService(tmp_path / "scan.json", tmp_path / "jobs.json")
    settings = ScannerSettings(use_mock_data=False)
    calls = {"count": 0}

    def build():
        calls["count"] += 1
        return {
            "generatedAt": f"2026-05-14T0{calls['count']}:00:00+00:00",
            "entry": [{"stockCode": str(calls["count"])}],
            "watch": [],
            "excluded": [],
        }

    first = service.get_or_refresh(settings, build, refresh_mode="auto", context={"ruleset": "v1"})
    second = service.get_or_refresh(settings, build, refresh_mode="auto", context={"ruleset": "v2"})

    assert calls["count"] == 2
    assert first["cacheStatus"]["cacheHit"] is False
    assert second["cacheStatus"]["cacheHit"] is False
    assert first["entry"][0]["stockCode"] == "1"
    assert second["entry"][0]["stockCode"] == "2"


def test_cache_only_with_empty_cache_does_not_call_builder(tmp_path):
    service = ScanCacheService(tmp_path / "scan.json", tmp_path / "jobs.json")
    settings = ScannerSettings(use_mock_data=False)
    calls = {"count": 0}

    def build():
        calls["count"] += 1
        return {"entry": [{"stockCode": "2330"}], "watch": [], "excluded": []}

    result = service.get_or_refresh(settings, build, refresh_mode="cache_only")

    assert calls["count"] == 0
    assert result["cacheStatus"]["cacheHit"] is False
    assert result["cacheStatus"]["refreshStatus"] == "cache_only_miss"
    assert result["entry"] == []
    assert not (tmp_path / "scan.json").exists()


def test_preexisting_empty_cache_is_treated_as_miss_and_rebuilt(tmp_path):
    cache_path = tmp_path / "scan.json"
    service = ScanCacheService(cache_path, tmp_path / "jobs.json")
    settings = ScannerSettings(use_mock_data=False)
    key = scan_cache_key(settings)
    cache_path.write_text(
        json.dumps(
            {
                "version": 1,
                "items": {
                    key: {
                        "storedAt": datetime.now(UTC).isoformat(),
                        "payload": {"entry": [], "watch": [], "excluded": [], "universeSize": 0},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    calls = {"count": 0}

    def build():
        calls["count"] += 1
        return {"entry": [], "watch": [{"stockCode": "2330"}], "excluded": [], "universeSize": 1}

    cache_only = service.get_or_refresh(settings, build, refresh_mode="cache_only")
    rebuilt = service.get_or_refresh(settings, build, refresh_mode="auto")

    assert cache_only["cacheStatus"]["cacheHit"] is False
    assert cache_only["cacheStatus"]["refreshStatus"] == "cache_only_miss"
    assert calls["count"] == 1
    assert rebuilt["watch"] == [{"stockCode": "2330"}]
    assert rebuilt["cacheStatus"]["cacheHit"] is False


def test_watch_only_scan_is_valid_and_cacheable(tmp_path):
    service = ScanCacheService(tmp_path / "scan.json", tmp_path / "jobs.json")
    settings = ScannerSettings(use_mock_data=False)
    payload = {"entry": [], "watch": [{"stockCode": "2330"}], "excluded": [], "universeSize": 1}

    first = service.get_or_refresh(settings, lambda: payload, refresh_mode="auto")
    cached = service.get_or_refresh(settings, lambda: {}, refresh_mode="cache_only")

    assert first["entry"] == []
    assert first["watch"] == [{"stockCode": "2330"}]
    assert cached["cacheStatus"]["cacheHit"] is True
    assert cached["watch"] == [{"stockCode": "2330"}]


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"entry": {}, "watch": [], "excluded": [], "universeSize": 1},
        {"entry": [{"stockCode": "2330"}], "watch": [], "excluded": [], "universeSize": 2},
        {"entry": [], "watch": [], "excluded": [], "universeSize": 0},
    ],
)
def test_first_scan_rejects_empty_or_inconsistent_payload_without_storing(tmp_path, payload):
    service = ScanCacheService(tmp_path / "scan.json", tmp_path / "jobs.json")
    settings = ScannerSettings(use_mock_data=False)

    with pytest.raises(ValueError, match="Scan result is empty or inconsistent"):
        service.get_or_refresh(settings, lambda: payload, refresh_mode="auto")

    assert not (tmp_path / "scan.json").exists()


def test_forced_empty_scan_preserves_last_good_payload(tmp_path):
    service = ScanCacheService(tmp_path / "scan.json", tmp_path / "jobs.json")
    settings = ScannerSettings(use_mock_data=False)

    service.get_or_refresh(
        settings,
        lambda: {"entry": [{"stockCode": "2330"}], "watch": [], "excluded": []},
        refresh_mode="auto",
    )

    with pytest.raises(ValueError, match="Scan result is empty or inconsistent"):
        service.get_or_refresh(
            settings,
            lambda: {
                "entry": [],
                "watch": [],
                "excluded": [],
                "universeSize": 0,
                "diagnostic": "private filesystem path C:/Users/example/secret.json",
            },
            build_refresh=lambda: {
                "entry": [],
                "watch": [],
                "excluded": [],
                "universeSize": 0,
                "diagnostic": "private filesystem path C:/Users/example/secret.json",
            },
            refresh_mode="force",
        )

    cached = service.get_or_refresh(settings, lambda: {}, refresh_mode="cache_only")
    assert cached["entry"] == [{"stockCode": "2330"}]
    assert "private filesystem path" not in (tmp_path / "scan.json").read_text(encoding="utf-8")


def test_publication_slot_policy_keeps_the_local_cache_fresh_until_the_next_slot():
    from backend.services import scan_cache as scan_cache_module
    from backend.services.cache_policy import next_refresh_after
    from backend.services.filing_calendar import market_closed_dates

    policy = {"strategy": "stale_while_revalidate", "reason": "routine_refresh", "schedule": "publication_slots",
              "minIntervalSeconds": 3600}
    stored = datetime(2026, 9, 15, 10, 40, tzinfo=UTC)  # Tue 18:40 Taipei

    expected = next_refresh_after(stored, market_closed_dates(2026))
    assert scan_cache_module._next_refresh(stored, policy) == expected
    assert expected == datetime(2026, 9, 16, 10, 0, tzinfo=UTC)  # Wed 18:00 Taipei, not an hour later


def test_stale_scan_cache_queues_single_background_refresh(tmp_path):
    service = ScanCacheService(tmp_path / "scan.json", tmp_path / "jobs.json")
    settings = ScannerSettings(use_mock_data=False)
    key = scan_cache_key(settings)
    service.store(
        key,
        settings,
        {
            "generatedAt": "2026-05-14T00:00:00+00:00",
            "entry": [{"stockCode": "2330"}],
            "watch": [],
            "excluded": [],
            "universeSize": 1,
        },
        {"strategy": "stale_while_revalidate", "reason": "test", "minIntervalSeconds": 1},
    )
    cache_path = tmp_path / "scan.json"
    cached = json.loads(cache_path.read_text(encoding="utf-8"))
    # Older than any market closure, so a publication slot has passed whatever today's date is
    # (one day back from a Sunday is Saturday, which is legitimately still fresh).
    cached["items"][key]["storedAt"] = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    cache_path.write_text(json.dumps(cached), encoding="utf-8")
    calls = {"count": 0}

    def refresh():
        calls["count"] += 1
        time.sleep(0.2)
        return {
            "generatedAt": "2026-05-14T01:00:00+00:00",
            "entry": [{"stockCode": "2454"}],
            "watch": [],
            "excluded": [],
        }

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


def test_empty_background_refresh_preserves_last_good_and_marks_job_failed(tmp_path, caplog):
    service = ScanCacheService(tmp_path / "scan.json", tmp_path / "jobs.json")
    settings = ScannerSettings(use_mock_data=False)
    key = scan_cache_key(settings)
    service.store(
        key,
        settings,
        {
            "generatedAt": "2026-05-14T00:00:00+00:00",
            "entry": [{"stockCode": "2330"}],
            "watch": [],
            "excluded": [],
            "universeSize": 1,
        },
        {"strategy": "stale_while_revalidate", "reason": "test", "minIntervalSeconds": 1},
    )
    cache_path = tmp_path / "scan.json"
    cached = json.loads(cache_path.read_text(encoding="utf-8"))
    # Older than any market closure, so a publication slot has passed whatever today's date is
    # (one day back from a Sunday is Saturday, which is legitimately still fresh).
    cached["items"][key]["storedAt"] = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    cache_path.write_text(json.dumps(cached), encoding="utf-8")

    def refresh():
        return {
            "entry": [],
            "watch": [],
            "excluded": [],
            "universeSize": 0,
            "diagnostic": "private filesystem path C:/Users/example/secret.json",
        }

    with caplog.at_level(logging.WARNING, logger="backend.services.scan_cache"):
        response = service.get_or_refresh(settings, refresh, build_refresh=refresh, refresh_mode="auto")
        assert service.wait_for_idle(timeout=3)

    status = service.status(settings)
    job = status["recentJobs"][-1]
    retained = service.get_or_refresh(settings, lambda: {}, refresh_mode="cache_only")

    assert response["entry"] == [{"stockCode": "2330"}]
    assert retained["entry"] == [{"stockCode": "2330"}]
    assert job["status"] == "failed"
    assert job["hasError"] is True
    assert "error" not in job
    assert "private filesystem path" not in str(status)
    assert "private filesystem path" not in caplog.text
    assert "private filesystem path" not in cache_path.read_text(encoding="utf-8")


def test_scan_cache_write_uses_unique_atomic_temp_file(tmp_path, monkeypatch):
    calls = []
    original_named_temp = scan_cache_module.tempfile.NamedTemporaryFile

    def recording_named_temp(*args, **kwargs):
        calls.append(kwargs)
        return original_named_temp(*args, **kwargs)

    monkeypatch.setattr(scan_cache_module.tempfile, "NamedTemporaryFile", recording_named_temp)
    service = ScanCacheService(tmp_path / "scan.json", tmp_path / "jobs.json")

    service._write_json(tmp_path / "scan.json", {"ok": True})

    assert json.loads((tmp_path / "scan.json").read_text(encoding="utf-8")) == {"ok": True}
    assert calls
    assert calls[0]["dir"] == tmp_path
    assert calls[0]["prefix"] == ".scan.json."
    assert calls[0]["suffix"] == ".tmp"
    assert calls[0]["delete"] is False
    assert list(tmp_path.glob("*.tmp")) == []
    assert list(tmp_path.glob(".*.tmp")) == []


def test_failed_background_refresh_redacts_error_details(tmp_path, caplog):
    service = ScanCacheService(tmp_path / "scan.json", tmp_path / "jobs.json")
    settings = ScannerSettings(use_mock_data=False)
    key = scan_cache_key(settings)
    service.store(
        key,
        settings,
        {
            "generatedAt": "2026-05-14T00:00:00+00:00",
            "entry": [{"stockCode": "2330"}],
            "watch": [],
            "excluded": [],
            "universeSize": 1,
        },
        {"strategy": "stale_while_revalidate", "reason": "test", "minIntervalSeconds": 1},
    )
    cache_path = tmp_path / "scan.json"
    cached = json.loads(cache_path.read_text(encoding="utf-8"))
    # Older than any market closure, so a publication slot has passed whatever today's date is
    # (one day back from a Sunday is Saturday, which is legitimately still fresh).
    cached["items"][key]["storedAt"] = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    cache_path.write_text(json.dumps(cached), encoding="utf-8")

    def refresh():
        raise RuntimeError("private filesystem path C:/Users/example/secret.json")

    with caplog.at_level(logging.WARNING, logger="backend.services.scan_cache"):
        service.get_or_refresh(settings, refresh, build_refresh=refresh, refresh_mode="auto")
        assert service.wait_for_idle(timeout=3)

    deadline = time.time() + 3
    status = service.status(settings)
    while status["recentJobs"][-1]["status"] != "failed" and time.time() < deadline:
        time.sleep(0.05)
        status = service.status(settings)

    job = status["recentJobs"][-1]
    assert job["status"] == "failed"
    assert job["hasError"] is True
    assert "error" not in job
    assert "private filesystem path" not in str(status)
    assert "Background scan cache refresh failed" in caplog.text
    assert "private filesystem path" not in caplog.text
    assert "Traceback" not in caplog.text


def test_parse_time_and_public_job_helpers():
    assert _parse_time(None) is None
    assert _parse_time("not-a-time") is None
    assert _parse_time("2026-01-01T00:00:00") == datetime.fromisoformat("2026-01-01T00:00:00")
    redacted = _public_job({"id": "j1", "error": "boom"})
    assert "error" not in redacted and redacted["hasError"] is True
    assert "hasError" not in _public_job({"id": "j1", "error": None})


def test_status_reloads_cache_when_file_signature_changes(tmp_path):
    service = ScanCacheService(tmp_path / "scan_cache.json", tmp_path / "refresh_state.json")
    service.status()  # primes the in-memory signature (file absent)
    (tmp_path / "scan_cache.json").write_text(
        json.dumps(
            {
                "version": 1,
                "items": {
                    "abc": {
                        "payload": {
                            "entry": [{"stockCode": "2330"}],
                            "watch": [],
                            "excluded": [],
                            "universeSize": 1,
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    service.status()  # signature changed -> reloads from disk
    assert "abc" in service._memory_cache


def test_status_ignores_invalid_disk_cache_before_first_cache_read(tmp_path):
    cache_path = tmp_path / "scan_cache.json"
    settings = ScannerSettings(use_mock_data=False)
    key = scan_cache_key(settings)
    stored_at = datetime.now(UTC).isoformat()
    cache_path.write_text(
        json.dumps(
            {
                "version": 1,
                "items": {
                    key: {
                        "storedAt": stored_at,
                        "payload": {"entry": [], "watch": [], "excluded": [], "universeSize": 0},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    service = ScanCacheService(cache_path, tmp_path / "refresh_state.json")

    status = service.status(settings)

    assert status["cacheKeys"] == 0
    assert status["selectedStoredAt"] is None
    assert status["selectedIsStale"] is None
    assert key not in service._memory_cache


def test_status_ignores_unselected_invalid_item_after_cache_read(tmp_path):
    cache_path = tmp_path / "scan_cache.json"
    settings = ScannerSettings(use_mock_data=False)
    valid_context = {"period": "2026Q1"}
    invalid_context = {"period": "2026Q2"}
    valid_key = scan_cache_key(settings, valid_context)
    invalid_key = scan_cache_key(settings, invalid_context)
    cache_path.write_text(
        json.dumps(
            {
                "version": 1,
                "items": {
                    valid_key: {
                        "storedAt": datetime.now(UTC).isoformat(),
                        "payload": {
                            "entry": [{"stockCode": "2330"}],
                            "watch": [],
                            "excluded": [],
                            "universeSize": 1,
                        },
                    },
                    invalid_key: {
                        "storedAt": datetime.now(UTC).isoformat(),
                        "payload": {"entry": [], "watch": [], "excluded": [], "universeSize": 0},
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    service = ScanCacheService(cache_path, tmp_path / "refresh_state.json")
    cached = service.get_or_refresh(
        settings,
        lambda: {},
        refresh_mode="cache_only",
        context=valid_context,
    )

    status = service.status(settings, invalid_context)

    assert cached["cacheStatus"]["cacheHit"] is True
    assert status["cacheKeys"] == 1
    assert status["selectedStoredAt"] is None
    assert status["selectedIsStale"] is None
    assert invalid_key not in service._memory_cache


def test_read_json_returns_fallback_for_missing_and_unreadable(tmp_path):
    service = ScanCacheService(tmp_path / "c.json", tmp_path / "s.json")
    assert service._read_json(tmp_path / "nope.json", {"x": 1}) == {"x": 1}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert service._read_json(bad, {"x": 2}) == {"x": 2}


def test_write_json_cleans_up_temp_on_replace_failure(tmp_path, monkeypatch):
    service = ScanCacheService(tmp_path / "c.json", tmp_path / "s.json")

    def fail_replace(self, target):
        raise OSError("replace failed")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        service._write_json(tmp_path / "c.json", {"version": 1})
    assert not list(tmp_path.glob(".c.json.*.tmp"))  # temp file removed


def test_write_json_swallows_temp_cleanup_failure(tmp_path, monkeypatch):
    service = ScanCacheService(tmp_path / "c.json", tmp_path / "s.json")

    def fail_replace(self, target):
        raise OSError("replace failed")

    def fail_unlink(self, missing_ok=False):
        raise OSError("cleanup failed")

    monkeypatch.setattr(Path, "replace", fail_replace)
    monkeypatch.setattr(Path, "unlink", fail_unlink)
    with pytest.raises(OSError, match="replace failed"):
        service._write_json(tmp_path / "c.json", {"version": 1})
