from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import tempfile
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Callable
from uuid import uuid4

logger = logging.getLogger(__name__)

from backend.models.settings import ScannerSettings
from backend.services.cache_policy import refresh_policy  # noqa: F401 — re-exported for callers


ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_SCAN_CACHE_PATH = ROOT_DIR / "data" / "market_scan_cache.json"
DEFAULT_REFRESH_STATE_PATH = ROOT_DIR / "data" / "cache_refresh_state.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _settings_payload(settings: ScannerSettings) -> dict:
    return settings.model_dump(mode="json")


def scan_cache_key(settings: ScannerSettings) -> str:
    payload = json.dumps(_settings_payload(settings), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


class ScanCacheService:
    def __init__(
        self,
        scan_cache_path: Path | str = DEFAULT_SCAN_CACHE_PATH,
        refresh_state_path: Path | str = DEFAULT_REFRESH_STATE_PATH,
    ) -> None:
        self.scan_cache_path = Path(scan_cache_path)
        self.refresh_state_path = Path(refresh_state_path)
        self._lock = RLock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="scan-cache-refresh")
        self._running: dict[str, Future] = {}
        self._memory_cache: dict[str, dict] = {}
        self._memory_cache_signature: tuple[int, int] | None = None

    def get_or_refresh(
        self,
        settings: ScannerSettings,
        build_sync: Callable[[], dict],
        build_refresh: Callable[[], dict] | None = None,
        refresh_mode: str = "auto",
    ) -> dict:
        key = scan_cache_key(settings)
        policy = refresh_policy()
        cached = self._get_item(key)

        if cached is None and refresh_mode == "cache_only":
            empty: dict = {"entry": [], "watch": [], "excluded": [], "universeSize": 0}
            return self._annotate(empty, key, None, False, policy, "cache_only_miss")

        if refresh_mode == "force" or cached is None:
            payload = build_sync() if refresh_mode != "force" or build_refresh is None else build_refresh()
            stored = self.store(key, settings, payload, policy)
            return self._annotate(stored["payload"], key, stored["storedAt"], False, policy, "completed_sync")

        stored_at = cached.get("storedAt")
        due = self._is_due(stored_at, policy)
        refresh_status = "fresh"
        if refresh_mode != "cache_only" and due:
            refresh_status = self._queue_refresh(key, settings, build_refresh or build_sync, policy)
        elif refresh_mode == "cache_only":
            refresh_status = "cache_only"
        return self._annotate(cached["payload"], key, stored_at, True, policy, refresh_status)

    def store(self, key: str, settings: ScannerSettings, payload: dict, policy: dict | None = None) -> dict:
        item = {
            "storedAt": utc_now(),
            "settings": _settings_payload(settings),
            "payload": copy.deepcopy(payload),
            "policy": policy or refresh_policy(),
        }
        with self._lock:
            cache = self._read_json(self.scan_cache_path, {"version": 1, "items": {}})
            cache.setdefault("items", {})[key] = item
            self._write_json(self.scan_cache_path, cache)
            self._memory_cache = copy.deepcopy(cache.get("items", {}))
            self._memory_cache_signature = self._file_signature(self.scan_cache_path)
        return item

    def status(self, settings: ScannerSettings | None = None) -> dict:
        key = scan_cache_key(settings) if settings else None
        with self._lock:
            signature = self._file_signature(self.scan_cache_path)
            if signature != self._memory_cache_signature:
                cache = self._read_json(self.scan_cache_path, {"version": 1, "items": {}})
                self._memory_cache = copy.deepcopy(cache.get("items", {}))
                self._memory_cache_signature = signature
            items = copy.deepcopy(self._memory_cache)
            state = self._read_json(self.refresh_state_path, {"version": 1, "jobs": []})
        jobs = state.get("jobs", [])
        selected = items.get(key) if key else None
        return {
            "strategy": "stale_while_revalidate",
            "cachePath": str(self.scan_cache_path),
            "refreshStatePath": str(self.refresh_state_path),
            "cacheKeys": len(items),
            "selectedCacheKey": key,
            "selectedStoredAt": selected.get("storedAt") if selected else None,
            "selectedIsStale": self._is_due(selected.get("storedAt"), refresh_policy()) if selected else None,
            "runningKeys": sorted(self._running.keys()),
            "recentJobs": jobs[-10:],
        }

    def _queue_refresh(self, key: str, settings: ScannerSettings, builder: Callable[[], dict], policy: dict) -> str:
        with self._lock:
            existing = self._running.get(key)
            if existing and not existing.done():
                return "running"
            job = {
                "id": uuid4().hex,
                "cacheKey": key,
                "status": "queued",
                "reason": policy["reason"],
                "queuedAt": utc_now(),
            }
            self._append_job(job)
            self._running[key] = self._executor.submit(self._run_refresh, key, settings, builder, policy, job["id"])
        return "queued"

    def _run_refresh(self, key: str, settings: ScannerSettings, builder: Callable[[], dict], policy: dict, job_id: str) -> None:
        self._update_job(job_id, status="running", startedAt=utc_now())
        try:
            payload = builder()
            stored = self.store(key, settings, payload, policy)
            self._update_job(
                job_id,
                status="success",
                finishedAt=utc_now(),
                storedAt=stored["storedAt"],
                universeSize=payload.get("universeSize"),
                entryCount=len(payload.get("entry", [])),
                watchCount=len(payload.get("watch", [])),
                excludedCount=len(payload.get("excluded", [])),
            )
        except Exception as exc:  # pragma: no cover - depends on network/runtime timing
            logger.exception("Background scan cache refresh failed for key=%s: %s", key, exc)
            self._update_job(job_id, status="failed", finishedAt=utc_now(), error=str(exc))
        finally:
            with self._lock:
                self._running.pop(key, None)

    def _annotate(self, payload: dict, key: str, stored_at: str | None, cache_hit: bool, policy: dict, refresh_status: str) -> dict:
        served = copy.deepcopy(payload)
        stored_time = _parse_time(stored_at)
        next_refresh = stored_time + timedelta(seconds=policy["minIntervalSeconds"]) if stored_time else None
        served["cacheStatus"] = {
            "strategy": "stale_while_revalidate",
            "source": "local_json_cache",
            "cacheKey": key,
            "cacheHit": cache_hit,
            "storedAt": stored_at,
            "servedAt": utc_now(),
            "isStale": self._is_due(stored_at, policy),
            "refreshStatus": refresh_status,
            "refreshReason": policy["reason"],
            "nextRefreshAfter": next_refresh.isoformat() if next_refresh else None,
        }
        return served

    def _get_item(self, key: str) -> dict | None:
        with self._lock:
            signature = self._file_signature(self.scan_cache_path)
            if signature == self._memory_cache_signature and key in self._memory_cache:
                return copy.deepcopy(self._memory_cache[key])
            cache = self._read_json(self.scan_cache_path, {"version": 1, "items": {}})
            self._memory_cache = copy.deepcopy(cache.get("items", {}))
            self._memory_cache_signature = signature
            item = cache.get("items", {}).get(key)
            return copy.deepcopy(item) if item else None

    @staticmethod
    def _file_signature(path: Path) -> tuple[int, int] | None:
        try:
            stat = path.stat()
        except FileNotFoundError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def _is_due(self, stored_at: str | None, policy: dict) -> bool:
        stored_time = _parse_time(stored_at)
        if not stored_time:
            return True
        return datetime.now(timezone.utc) >= stored_time.astimezone(timezone.utc) + timedelta(seconds=policy["minIntervalSeconds"])

    def _append_job(self, job: dict) -> None:
        with self._lock:
            state = self._read_json(self.refresh_state_path, {"version": 1, "jobs": []})
            jobs = state.setdefault("jobs", [])
            jobs.append(job)
            state["jobs"] = jobs[-100:]
            self._write_json(self.refresh_state_path, state)

    def _update_job(self, job_id: str, **updates) -> None:
        with self._lock:
            state = self._read_json(self.refresh_state_path, {"version": 1, "jobs": []})
            for job in state.setdefault("jobs", []):
                if job.get("id") == job_id:
                    job.update(updates)
                    break
            self._write_json(self.refresh_state_path, state)

    def _read_json(self, path: Path, fallback: dict) -> dict:
        try:
            if not path.exists():
                return copy.deepcopy(fallback)
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return copy.deepcopy(fallback)

    def _write_json(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                tmp_path = Path(handle.name)
                handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
                handle.flush()
                os.fsync(handle.fileno())
            tmp_path.replace(path)
        except OSError:
            if tmp_path is not None:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise
