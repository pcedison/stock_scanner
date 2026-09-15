"""Market refresh jobs for the local FastAPI backend.

The Cloudflare Worker records a refresh job in D1 and a GitHub Actions run rebuilds the
R2 seed. Local development has no queue and no workflow, so the same command API is
served by running the rebuild on a single background thread. The public payloads and the
``queued``/``running``/``success``/``failed`` vocabulary mirror
``cloudflare/worker_refresh_jobs.py`` because ``frontend/market_refresh.js`` validates the
exact key set of both responses.

Fields the Worker only has because GitHub Actions does the work (``ownerRunId``,
``ownerRunUrl``, ``dispatchStatus``, ``dispatchErrorCode``) are omitted here rather than
faked; the client treats all of them as optional.
"""

from __future__ import annotations

import logging
import re
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import RLock
from uuid import uuid4

logger = logging.getLogger(__name__)

IDEMPOTENCY_KEY_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,80}\Z", re.ASCII)
JOB_ID_PATTERN = re.compile(r"[0-9a-f]{32}\Z", re.ASCII)
JOB_REASONS = frozenset({"financial_report_window", "monthly_revenue_window", "routine_refresh"})
ACTIVE_STATUSES = frozenset({"queued", "running"})
MAX_TRACKED_JOBS = 32


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def normalize_idempotency_key(value: str | None) -> str | None:
    if value is None:
        return None
    if IDEMPOTENCY_KEY_PATTERN.fullmatch(value) is None:
        raise ValueError("Idempotency-Key must be 1..80 ASCII letters, digits, '.', '_', ':', or '-'")
    return value


def _public_job(job: dict) -> dict:
    payload: dict = {"jobId": job["id"], "status": job["status"]}
    if job["reason"] is not None:
        payload["reason"] = job["reason"]
    for key in ("queuedAt", "startedAt", "finishedAt"):
        if job[key] is not None:
            payload[key] = job[key]
    payload["updatedAt"] = job["finishedAt"] or job["startedAt"] or job["queuedAt"]
    payload["hasError"] = job["hasError"]
    return payload


class RefreshJobService:
    """Single-flight background refresh jobs with Worker-shaped public payloads."""

    def __init__(self, max_jobs: int = MAX_TRACKED_JOBS) -> None:
        self._lock = RLock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="market-refresh-job")
        self._jobs: OrderedDict[str, dict] = OrderedDict()
        self._max_jobs = max(1, max_jobs)

    def start(
        self, run: Callable[[], object], *, scope: str, reason: str, idempotency_key: str | None = None
    ) -> dict:
        """Queue ``run`` unless the same key, or an active job for ``scope``, already covers it."""
        with self._lock:
            existing = self._reusable_job(scope, idempotency_key)
            if existing is not None:
                return _public_job(existing)
            job_id = uuid4().hex
            job: dict = {
                "id": job_id,
                "scope": scope,
                "idempotencyKey": idempotency_key,
                "status": "queued",
                "reason": reason if reason in JOB_REASONS else None,
                "queuedAt": utc_now(),
                "startedAt": None,
                "finishedAt": None,
                "hasError": False,
            }
            self._jobs[job_id] = job
            self._evict_finished_jobs()
            self._executor.submit(self._run, job_id, run)
            return _public_job(job)

    def get(self, job_id: str) -> dict | None:
        if JOB_ID_PATTERN.fullmatch(job_id or "") is None:
            return None
        with self._lock:
            job = self._jobs.get(job_id)
            return _public_job(job) if job is not None else None

    def _evict_finished_jobs(self) -> None:
        # Only terminal jobs are droppable: evicting a queued/running one would 404 the
        # client that is still polling it. The list grows past max_jobs rather than that.
        for job_id, job in list(self._jobs.items()):
            if len(self._jobs) <= self._max_jobs:
                return
            if job["status"] not in ACTIVE_STATUSES:
                del self._jobs[job_id]

    def _reusable_job(self, scope: str, idempotency_key: str | None) -> dict | None:
        if idempotency_key is not None:
            for job in reversed(self._jobs.values()):
                if job["scope"] == scope and job["idempotencyKey"] == idempotency_key:
                    return job
        for job in reversed(self._jobs.values()):
            if job["scope"] == scope and job["status"] in ACTIVE_STATUSES:
                return job
        return None

    def _run(self, job_id: str, run: Callable[[], object]) -> None:
        self._update(job_id, status="running", startedAt=utc_now())
        try:
            run()
        except BaseException as exc:  # noqa: BLE001 - a job must never be left "running"
            logger.warning("Market refresh job %s failed; the last cached scan is unchanged.", job_id)
            self._update(job_id, status="failed", finishedAt=utc_now(), hasError=True)
            if not isinstance(exc, Exception):
                raise
        else:
            self._update(job_id, status="success", finishedAt=utc_now())

    def _update(self, job_id: str, **updates: object) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job.update(updates)
