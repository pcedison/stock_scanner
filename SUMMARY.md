# 事件報告:正式環境快取凍結與部署管線修復(2026-06-01)

## 一、緣起

使用者回報:今天已是 6/1,但網頁上的市場掃描快取仍停在 5/30,且狀態一直顯示
「快取狀態:先讀已存快取／刷新狀態:已排入背景更新／資料時間:2026/5/30」。
疑問是「快取是否沒有自動跨日刷新?會不會造成資訊嚴重延遲?」

結論:**會嚴重延遲**,且實測時 R2 manifest 已凍結約 57 小時。但根因不是快取邏輯,
而是 CI/部署管線的鎖死。

## 二、系統架構(關鍵背景)

正式環境是 Cloudflare Python Worker(`cloudflare/worker.py`)+ R2(快取)+ D1(資料庫),
採 stale-while-revalidate:

1. 使用者打 `/api/scan/market/index` 與 `/api/scan/market/results`(v2 分頁讀取)→ Worker
   直接回 R2 既有快取;明確要求重新整理時打 `/api/scan/market/refresh`,Worker 才往 D1
   `refresh_jobs` 塞一筆 `queued`。**Worker 自己不抓資料、不重建 manifest。**
   (整包掃描的 v1 `GET/POST /api/scan/market` 已於 2026-09-15 退場,現在回 404。)
2. 真正重建 R2 manifest 的是 GitHub Actions `cloudflare-r2-seed-refresh.yml`(每 15 分鐘):
   撈 queued job → 標 running → `build_cloudflare_seed.py`(線上抓 TWSE/MOPS)→ 上傳 R2 → 標 success。
3. 部署由 `cloudflare-deploy.yml`(push main 觸發)負責:D1 migration、部署 Worker/Pages、
   部署後健康檢查、失敗回滾。

`cloudflare-deploy` 與 `cloudflare-r2-seed-refresh` **刻意共用 `cloudflare-production`
concurrency group**(見 `docs/cloudflare_deployment.md`),目的是防止「部署」與「R2 上傳」
同時動正式環境而競態;此共用關係有 `check_operational_readiness.py` + 測試護欄強制。

## 三、根因

`production` GitHub environment 設了 `required_reviewers`(只有維護者自己,屬「團隊模式」
誤用在 solo 倉庫)。PR #50 的部署(commit `dfbf616`)自 **2026-05-29 19:26** 起卡在
`waiting` 等人工核准達 **2.5 天**;因共用組設 `cancel-in-progress: false`,這個等待中的部署
**一直持有 `cloudflare-production` 鎖**,把後續每 15 分鐘的快取刷新全部擋成 pending →
互相取消(近 30 次 29 cancelled、0 success)。

時間完全吻合:卡住點 5/29 19:26 ≈ manifest 凍結點 5/29 18:59。

**連帶影響**:#50~#80 的部署同樣被排在後面取消,正式環境因此 2.5 天未成功部署,
線上跑的是 #50 之前的舊 Worker。

## 四、處理經過

### 急救(讓資料先追上)
- `gh run cancel 26657662037` 取消那個卡住的過時部署 → 鎖一放開,
  手動觸發的強制刷新立即開始執行並成功 → 線上資料追上 6/1、`isStale: False`。

### 根治(讓問題不再發生)
- 採文件本身對 solo 維護者的指引:**移除 `production` 的 `required_reviewers`**
  (`gh api PUT .../environments/production` 把 reviewers 設 null)。純 GitHub 設定變更,
  保留共用組防競態不變量,未動任何程式碼/測試。
- 已確認 main 有 branch protection(`enforce_admins: true` + 必要狀態檢查
  `validate`/CodeQL)作為 solo 安全網,移除核准閘門後仍有把關。

### 部署管線補強(PR #84)
- 移除核准閘門後改為「merge main 自動部署」。但首次補部署 #50~#84 時,
  在「Verify deployed Pages frontend」步驟失敗並回滾 Worker——原因是
  `check_pages_frontend.py` **單次抓取** pages.dev,不容忍 Cloudflare Pages 部署後
  數秒~一分鐘的傳播延遲(抓到舊 HTML 即失敗)。自動部署後此 flake 會讓每次 merge
  都可能隨機回滾。
- 修法:`validate_pages_frontend()` 加入容錯重試(對抓取失敗與資產未現都重試),
  workflow 改用 `--attempts 6 --retry-delay 10`(容忍約 60 秒)。補 3 個測試。

### 上線
- #50~#84 全數部署完成,連續 3 次部署 success,線上 health ok、`cacheQuality.ok: True`。

## 五、PR 處理

| PR | 內容 | 處置 |
|---|---|---|
| #84 | 部署後 Pages 驗證加傳播容錯重試 | 已合併 + 部署成功 |
| #83 | 快取狀態診斷工具 `scripts/diagnose_cache_status.py` | 已合併 + 部署成功 |
| #82 | 前端依賴升級(2 項) | 已合併 + 部署成功 |
| #81 | Python 依賴升級(6 項) | **未合併**:依賴衝突 |

### #81 未合併原因
`pydantic-core` 被獨立升到 `2.47.0`,但 `<3` 範圍內最新的 `pydantic 2.13.4` 仍鎖死
`pydantic-core==2.46.4`(pydantic-core 是 pydantic 的傳遞依賴),導致 `ResolutionImpossible`。
根本解:在 `.github/dependabot.yml` 讓 dependabot 不要獨立升傳遞依賴
(`ignore: pydantic-core` 或限制 `dependency-type: direct`),再讓其重建群組 PR。
另注意該 PR 含 `mypy 1.20.2 → 2.1.0` 跨大版號,建議單獨驗證 mypy gate。

## 六、交付物與後續可用工具

- **`scripts/diagnose_cache_status.py`**:可隨時 `--url <部署網址>/api/cache/status` 巡檢快取健康;
  能分辨 `ok` / `queued_never_consumed`(刷新消費端被鎖死)/ `refresh_failing` 等情境。
- **`scripts/check_pages_frontend.py`**:部署後驗證已具傳播容錯重試。

## 七、異地管理快速排查指引

1. 快取看起來沒更新 → 跑 `python scripts/diagnose_cache_status.py --url https://stock-scanner-beta-api.pcedison.workers.dev/api/cache/status`。
2. 若判讀為 `queued_never_consumed` → 多半是刷新管線被卡。先查
   `gh run list --status waiting`(是否有部署卡在等核准/環境保護)與
   `gh run list --workflow cloudflare-r2-seed-refresh.yml`(是否大量 cancelled)。
3. 需要立即補資料 → `gh workflow run cloudflare-r2-seed-refresh.yml -f force=true`。
4. 部署 / 刷新共用 `cloudflare-production` 組是刻意的防競態設計,勿輕易拆組;
   要避免「等待中的部署餓死刷新」,正解是不要讓部署長時間卡在等核准。

## 八、最終結果

- 線上資料時間 6/1、快取健康(`isStale: False`)。
- 正式環境已是最新 Worker(#50~#84 全部上線)。
- 部署管線:核准閘門移除後自動部署、Pages 驗證具傳播容錯,連續部署成功。
- 唯一未竟事項:#81 依賴衝突,已記錄根因與建議,待依上述根本解處理。
