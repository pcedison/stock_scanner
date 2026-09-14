# Align refresh cadence with the TWSE filing calendar and source publication times

Date: 2026-09-14. Stacked on PR #162 (TPEX TLS intermediate + trading-hour health age).

## Evidence

### Statutory deadlines (證券交易法 §36, FSC 公開發行公司財務報告及營運情形公告申報特殊適用範圍辦法, TWSE notices)

| Report | General listed/OTC | Financial holding / bank / insurance | KY (primary-listed foreign) |
|---|---|---|---|
| Annual (Q4) | 3/31 (paid-in ≥ NT$10bn: 75 days ≈ 3/16) | 3/31 | 3/31 |
| Q1 | 5/15 | 5/30 | 5/15 |
| Q2 (half-year) | **8/14** | 8/31 | 8/31 |
| Q3 | 11/14 | **11/29** | 11/14 |
| Monthly revenue | by the **10th** of the next month | insurance: by the 15th | exempt if secondary-listed |

A deadline on a non-business day moves to the next business day (e.g. 2025-11-29 Sat → 12/01).

### Observed source publication (HTTP Last-Modified / ETag, 2026-09-14)

| Source | Regenerated (Taipei) | Content |
|---|---|---|
| TWSE OpenAPI `t187ap03_L`, `t187ap05_L`, `t187ap06/07_L_*` | ≈ 05:25 daily batch | MOPS filings through the previous day |
| TPEX OpenAPI `mopsfin_t187ap03/05/06/07_O*`, `tpex_mainboard_peratio_analysis` | ≈ 16:00 | same-day filings; P/E dated the previous trading day |
| TWSE `BWIBBU_d` | after the close, same day (present at 17:40) | same-day valuation ratios |
| MOPS (`mops.twse.com.tw`) | real time | used only by the history backfill |

None of the OpenAPI datasets change intraday, so any rebuild between two regenerations re-fetches identical data.

## Problems found

1. **Wrong deadlines.** `filing_calendar` / `cache_policy` / `scheduler` / Worker mirror use Q2 = 8/31 for everyone and omit Q3 financial 11/29; no holiday extension.
2. **Filing windows miss the filing peaks.** Financial window is deadline ±3 days (the 3 days *after* a deadline receive almost nothing; the 2 weeks before, when most companies file, are "routine"). Monthly-revenue window is day 8-15 (days 1-7 are missed, 11-15 are mostly empty).
3. **Interval cadence is not anchored to publication.** Deadlines are `generatedAt + 2h/3h/12h`, plus a 120-minute refresh-ahead: builds land at arbitrary times (e.g. 01:10, 13:10) and re-fetch unchanged datasets; up to 12 full rebuilds (~15 min each) per window day.
4. **GitHub backstop runs 46×/weekday** (every 20 min 08-19 Taipei + hourly) although the Worker cron is primary.
5. **Failure cooldown is policy-interval based** (up to 10 h), so after a failure retries come from the 20-minute GitHub backstop and each one is a full rebuild.
6. **`r2_refresh_decision early-check` uses a 36 h wall-clock age**, forcing a Monday-morning rebuild before any new data exists (same bug class as the health monitor).
7. **Duplicated policy in `cloudflare/worker.py`** drifts from the backend.
8. **Nightly date sweep** runs 10 jobs every day; its pinned dates only change when date code changes.

## Plan (TDD, one PR)

1. `filing_calendar`: correct deadlines (Q2 general 8/14 + financial 8/31, Q3 financial 11/29), holiday extension for window ends / freshness / revenue lag. Freshness still keys on the *final* (financial) deadline.
2. `cache_policy`: publication slots — morning 06:30 (TWSE batch) and evening 18:00 (TPEX batch + BWIBBU, confirmed present by 17:40) on trading days. Evening slot always; morning slot only inside filing windows (monthly revenue: day 1 → extended 10th; financial: general deadline − 14 days → extended final deadline; annual: 3/1 → extended 3/31). `next_refresh_after(generatedAt, closedDates)` = first active slot after the build. Seed build writes it to the manifest.
3. Worker: trust manifest `nextRefreshAfter` (single source of truth); fallback to the old rule for legacy manifests. Refresh-ahead 120 min → 0 (never build before publication). Terminal-job cooldown 1 h (failure backoff; success is already fresh until the next slot). Crons cover the slot windows including Monday 06:30 (UTC Sunday).
4. GitHub backstop schedule: 3 runs per weekday just after the slots. `r2_refresh_decision` age → trading hours, refresh-ahead 0.
5. `scheduler.should_wake_up` uses the same windows.
6. Nightly date sweep → weekly + on PRs touching date logic.
7. Docs.

Out of scope (YAGNI): source-fingerprint skip-if-unchanged — with at most two slot-aligned builds per trading day the remaining saving is small.
