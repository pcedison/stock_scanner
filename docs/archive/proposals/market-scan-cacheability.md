# 提案:`/api/scan/market` 可快取化

狀態:**Phase 1 實作中**（雙端新增 GET + 契約測試）。
日期:2026-05-30。

## 問題

`/api/scan/market` 是 **POST**,而 Cloudflare edge cache 預設只快取 GET/HEAD。這是最重、呼叫最頻繁的端點:

- 前端在頁面載入時即 `refreshMarketScan({refreshMode:"auto"})`(`frontend/app.js`),**每次開頁都打一次**。
- 每次請求都會:喚起 Worker → 讀 `manifest.json` + `market_scan_summary.json`(2 次 R2 Class B)→ 解析大 JSON(吃 Worker CPU)→ 跑 `cache_policy()` →(stale 時)寫 D1 排程。
- 因為是 POST,**完全無法被 edge 擋下**:資料一整天沒變,N 個使用者開頁就是 N 次完整 Worker + R2。

對免費方案(Workers 10ms CPU、D1 100k writes/day、R2 10M Class B/月)而言,這是**單一最大的佔用削減點**。

## 關鍵觀察

- Worker 的 `_route_scan` 只從 POST body 取 `refreshMode`;`settings` 被忽略,掃描結果一律來自全域 R2 seed → **回應是全域、無狀態的**,本質可被所有人共用快取。
- `cache_policy()` 已算出 `minIntervalSeconds`(7200 / 10800 / 43200),正好可當 edge cache TTL / SWR 視窗。
- 兩端 payload shape **本就不同**(Worker 回 compact seed + `detailMode`;FastAPI 回 local provider 的完整 scan),這也是 `scan/market` 至今未納入契約測試的原因。

## 唯二阻礙

1. **方法是 POST**(不可快取)。
2. **讀取與副作用耦合**:`ensure_refresh_job()` 在 stale 時會 queue 一筆 D1 job。可被 edge 快取的純讀 GET **不能**帶這種寫副作用(被快取的請求根本不會抵達 Worker,行為會不一致)。

## 設計:讀寫分離

- 新增 **`GET /api/scan/market`**(純讀):回傳全域掃描 + `cacheStatus`,帶 `public_cache_seconds`(取自 `cache_policy().minIntervalSeconds`)+ `stale-while-revalidate`。**不**呼叫 `ensure_refresh_job`。→ TTL 內重複載入由 edge 直接回應,Worker CPU 0、R2 0、D1 0。
- 保留 **`POST /api/scan/market`**(force / 明確刷新):維持現有「讀 + 視 stale queue refresh job」行為。
- refresh 觸發改由「使用者 force(POST)」+「既有 15 分鐘 cron」負責,不再依賴頁面載入。

## 分階段

- **Phase 1(本 PR)**:雙端新增 `GET /api/scan/market` 純讀端點 + 契約測試(以子集 key 斷言核心類別 `entry/watch/excluded`)。POST 與前端維持不動 → 純新增、零行為破壞。
- **Phase 2**:前端載入時的 auto 由 POST 改打新 GET;只有手動 force 用 POST。觀察 edge cache 命中與資源下降。
- **Phase 3(視需要)**:微調 TTL 策略、`cacheStatus` 欄位、monitoring。

## 雙端同步與安全

- FastAPI 與 Worker 都新增 GET 分支,`tests/test_api_worker_contracts.py` 同步覆蓋。
- GET 為純讀無副作用 → 不需 CSRF(CSRF 只擋 `UNSAFE_API_METHODS`,GET 不在其中)。
- 不做寫操作 / 不觸發 D1 job。

## 風險 / 取捨

- Phase 2 後,auto 載入不再「順便 queue refresh」;靠 15 分鐘 cron + 使用者 force 補足,需確認資料新鮮度 SLA 可接受。
- edge TTL 期間 force-refresh 更新 R2,GET 仍可能回舊資料至 TTL 到期(SWR 緩解;force 走未快取的 POST)。

## 預期效益

頁面載入的 market-scan 在 TTL 內 **0 Worker 調用、0 R2 read、0 D1**(由 edge 回應),直接削減免費方案下最大的重複佔用。
