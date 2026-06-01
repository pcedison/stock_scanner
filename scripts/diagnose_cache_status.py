"""診斷部署環境的 /api/cache/status,判斷快取為何沒有跨日刷新。

背景:正式環境(Cloudflare Worker)採 stale-while-revalidate。使用者開頁面時,
Worker 只會往 D1 的 refresh_jobs 塞一筆 queued job,真正重建 R2 manifest 的是
GitHub Actions(cloudflare-r2-seed-refresh)。若消費端沒認領 job、或一直失敗,
manifest 就會凍結、資料時間卡住不動。

本工具把 /api/cache/status 的回應抽成 analyze_cache_status() 純函式,輸出一份
可讀的中文判讀與建議動作,並可作為 CLI 直接打線上端點巡檢。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

CHECK_USER_AGENT = "Mozilla/5.0 stock-scanner-cache-diagnose/1.0"

# 預設門檻:manifest 超過 36 小時視為過舊;queued job 卡超過 30 分鐘沒人認領視為消費端異常。
DEFAULT_MAX_CACHE_AGE_HOURS = 36.0
DEFAULT_QUEUED_STALL_MINUTES = 30.0

# 判讀代碼 -> 是否健康。供 CLI 決定離開碼。
_HEALTHY_VERDICTS = {"ok"}


def validate_cache_status_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc or not parsed.path.endswith("/api/cache/status"):
        raise RuntimeError("Cache status URL must be an https URL ending in /api/cache/status")
    return url


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _hours_since(value: Any, now: datetime) -> float | None:
    parsed = _parse_timestamp(value)
    if parsed is None:
        return None
    return max(0.0, (now - parsed).total_seconds() / 3600)


def _minutes_since(value: Any, now: datetime) -> float | None:
    hours = _hours_since(value, now)
    return None if hours is None else hours * 60


def analyze_cache_status(
    payload: dict[str, Any],
    *,
    now: datetime | None = None,
    max_cache_age_hours: float = DEFAULT_MAX_CACHE_AGE_HOURS,
    queued_stall_minutes: float = DEFAULT_QUEUED_STALL_MINUTES,
) -> dict[str, Any]:
    """把 /api/cache/status 回應判讀成結構化結論。

    回傳欄位:
      verdict   判讀代碼(ok / stale_manifest / queued_never_consumed /
                refresh_failing / refresh_in_progress / invalid_payload)
      healthy   是否健康(verdict == ok)
      message   中文判讀
      action    建議動作(健康時為 None)
      details   佐證數據(資料時間、過期幾小時、各狀態 job 數等)
    """
    current = (now or datetime.now(UTC))
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    current = current.astimezone(UTC)

    market = payload.get("marketScan")
    if not isinstance(market, dict):
        return {
            "verdict": "invalid_payload",
            "healthy": False,
            "message": "回應缺少 marketScan 區塊,無法判讀快取狀態。",
            "action": "確認 URL 指向正確的 /api/cache/status 端點。",
            "details": {},
        }

    stored_at = market.get("storedAt")
    age_hours = _hours_since(stored_at, current)
    is_stale = bool(market.get("isStale"))

    jobs = payload.get("recentJobs")
    jobs = jobs if isinstance(jobs, list) else []
    status_counts: dict[str, int] = {}
    for job in jobs:
        key = str(job.get("status") or "unknown")
        status_counts[key] = status_counts.get(key, 0) + 1

    # 是否有任何 job 真的被消費端認領過(owner_run_id 有值,代表 GitHub Actions 接手)。
    consumed_any = any(job.get("owner_run_id") or job.get("ownerRunId") for job in jobs)
    queued_jobs = [job for job in jobs if str(job.get("status")) == "queued"]
    running_jobs = [job for job in jobs if str(job.get("status")) == "running"]
    failed_jobs = [job for job in jobs if str(job.get("status")) == "failed" or job.get("hasError")]

    # queued job 最久卡了多少分鐘(取最舊一筆)。
    oldest_queued_minutes: float | None = None
    for job in queued_jobs:
        minutes = _minutes_since(job.get("queued_at") or job.get("queuedAt"), current)
        if minutes is None:
            continue
        if oldest_queued_minutes is None or minutes > oldest_queued_minutes:
            oldest_queued_minutes = minutes

    details = {
        "storedAt": stored_at,
        "ageHours": round(age_hours, 2) if age_hours is not None else None,
        "isStale": is_stale,
        "maxCacheAgeHours": max_cache_age_hours,
        "refreshStatus": market.get("refreshStatus"),
        "jobStatusCounts": status_counts,
        "queuedCount": len(queued_jobs),
        "runningCount": len(running_jobs),
        "failedCount": len(failed_jobs),
        "consumedAny": consumed_any,
        "oldestQueuedMinutes": round(oldest_queued_minutes, 1) if oldest_queued_minutes is not None else None,
        "latestRevenuePeriod": market.get("latestRevenuePeriod"),
        "latestFinancialPeriod": market.get("latestFinancialPeriod"),
    }

    age_text = f"{age_hours:.1f} 小時前" if age_hours is not None else "時間不明"
    too_old = age_hours is not None and age_hours > max_cache_age_hours
    fresh = not is_stale and not too_old

    # 健康:manifest 未過期且未超齡。
    if fresh:
        return {
            "verdict": "ok",
            "healthy": True,
            "message": f"快取健康。資料時間為 {age_text},尚在有效期限內。",
            "action": None,
            "details": details,
        }

    # 消費端正在處理(已有 job 被認領為 running)。
    if running_jobs:
        return {
            "verdict": "refresh_in_progress",
            "healthy": False,
            "message": f"快取已過期({age_text}),但已有背景刷新工作正在執行,稍候應會更新。",
            "action": "等候數分鐘後重新查詢;若長時間停在 running,檢視對應 GitHub Actions run。",
            "details": details,
        }

    # 最近一筆已被認領但失敗 -> 消費端有跑但壞掉。
    if failed_jobs and consumed_any:
        return {
            "verdict": "refresh_failing",
            "healthy": False,
            "message": f"快取已過期({age_text}),背景刷新工作被認領後失敗,manifest 未更新。",
            "action": "檢視 cloudflare-r2-seed-refresh workflow 失敗 log(多半是線上資料源抓取或 R2 上傳出錯)。",
            "details": details,
        }

    # 有 queued job、卻從沒有任何 job 被認領,且最舊的已卡超過門檻 -> 消費端根本沒在認領。
    if queued_jobs and not consumed_any and (oldest_queued_minutes is None or oldest_queued_minutes >= queued_stall_minutes):
        stall_text = (
            f"最舊一筆已堆積 {oldest_queued_minutes:.0f} 分鐘" if oldest_queued_minutes is not None else "已持續堆積"
        )
        return {
            "verdict": "queued_never_consumed",
            "healthy": False,
            "message": (
                f"快取已過期({age_text}):D1 有 {len(queued_jobs)} 筆 queued 刷新工作"
                f"({stall_text}),但沒有任何一筆被消費端認領(owner_run_id 全為空)。"
                "代表 GitHub Actions 刷新管線沒有跑完,每次開頁面只是再堆一筆沒人消化的工作。"
            ),
            "action": (
                "檢查 cloudflare-r2-seed-refresh workflow 是否被 cancelled/卡 pending"
                "(常見於與 cloudflare-deploy 共用 concurrency group 互相取消);"
                "或手動 dispatch 一次強制刷新。"
            ),
            "details": details,
        }

    # 過期但 job 訊號不明確(例如沒有任何 job 或剛 queued 還沒到門檻)。
    return {
        "verdict": "stale_manifest",
        "healthy": False,
        "message": f"快取已過期({age_text}),目前沒有明確的背景刷新進度訊號。",
        "action": "重新整理頁面以觸發一筆刷新工作,並確認刷新管線是否正常運作。",
        "details": details,
    }


def _load_json_url(url: str, timeout: int) -> dict[str, Any]:
    safe_url = validate_cache_status_url(url)
    request = Request(safe_url, headers={"User-Agent": CHECK_USER_AGENT})
    try:
        # validate_cache_status_url 已限制 scheme/結尾路徑後才呼叫 urlopen。
        with urlopen(request, timeout=timeout) as response:  # nosec B310
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError(f"Cache status returned HTTP {exc.code}: {safe_url}") from exc
    except URLError as exc:
        raise RuntimeError(f"Cache status failed to connect: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Cache status did not return valid JSON: {safe_url}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="診斷部署環境 /api/cache/status,判斷快取為何沒有跨日刷新。")
    parser.add_argument("--url", required=True, help="完整的部署 /api/cache/status URL")
    parser.add_argument(
        "--max-cache-age-hours",
        type=float,
        default=DEFAULT_MAX_CACHE_AGE_HOURS,
        help="manifest 超過此小時數視為過舊(預設 36)",
    )
    parser.add_argument(
        "--queued-stall-minutes",
        type=float,
        default=DEFAULT_QUEUED_STALL_MINUTES,
        help="queued job 卡超過此分鐘數且無人認領,視為消費端異常(預設 30)",
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 輸出完整判讀結果")
    parser.add_argument("--timeout", type=int, default=20)
    args = parser.parse_args(argv)

    try:
        payload = _load_json_url(args.url, args.timeout)
    except RuntimeError as exc:
        print(f"快取狀態診斷失敗:{exc}", file=sys.stderr)
        return 2

    result = analyze_cache_status(
        payload,
        max_cache_age_hours=args.max_cache_age_hours,
        queued_stall_minutes=args.queued_stall_minutes,
    )

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"判讀:{result['verdict']}（{'健康' if result['healthy'] else '異常'}）")
        print(result["message"])
        if result["action"]:
            print(f"建議:{result['action']}")

    return 0 if result["verdict"] in _HEALTHY_VERDICTS else 1


if __name__ == "__main__":
    raise SystemExit(main())
