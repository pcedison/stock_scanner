# Cloudflare 部署須知

Beta 0.0.5 目前採用 Cloudflare Pages + Python Workers + D1 + R2。

## 已建立的 Cloudflare 資源

- Pages：`stock-scanner-beta`
- Production URL：`https://stock-scanner-beta.pages.dev`
- Worker API：`stock-scanner-beta-api`
- Worker URL：`https://stock-scanner-beta-api.pcedison.workers.dev`
- D1：`stock-scanner-beta-db`
- R2：`stock-scanner-beta-cache`

Pages 透過 `frontend/functions/api/[[path]].js` 將 `/api/*` 代理到 Worker。前端仍使用同源 `/api`，所以本機 FastAPI 與 Cloudflare Worker 可以並存。

## 快取策略

- R2 保存 `public/market_scan_latest.json`、`public/analysis_by_code.json`、公司清單與資料來源狀態。
- R2 也保存 `official/official_fundamentals_history.json`、`official/official_history_backfill_progress.json` 與壓縮種子檔。
- Git repo 只提交壓縮後的 `data/official_cache_seed_2026-05-14.zip` 與 sha256，不提交大型展開 JSON。
- GitHub Actions 會從壓縮快取重建 Cloudflare seed，不會每次 push 都重新全量爬官方網站。
- 掃描採用 stale-while-revalidate：使用者按搜尋時先讀 R2 或本地 JSON 快取立即回傳；若 manifest 已超過更新窗口，系統只建立一筆 refresh job，避免多人同時觸發重複抓取。
- 本機 FastAPI 會用單一背景 worker 做增量刷新並寫入 `data/market_scan_cache.json`；Cloudflare 版會在 D1 `refresh_jobs` 記錄待刷新工作，由排程 GitHub Actions 低頻重建 seed 並標記完成。
- 公告窗口會縮短更新間隔：財報期限前後約 2 小時檢查一次，月營收窗口約 3 小時一次，平時約 12 小時一次。

## 一次性人工介入

GitHub Actions 需要兩個 repository secrets：

- `CLOUDFLARE_ACCOUNT_ID`：`34d97898ae94d67b3ba74d3e09b82cc5`
- `CLOUDFLARE_API_TOKEN`：Cloudflare API Token，需具備 Workers Scripts、Workers Tail、Pages、D1、R2 的編輯權限。

目前這個 repo 已先設定 `CLOUDFLARE_ACCOUNT_ID`；仍需人工建立並新增 `CLOUDFLARE_API_TOKEN`。建議在 Cloudflare 建立一個專用 Token，只給這個帳號與這個專案需要的最小權限。Token 建立後到 GitHub repo 的 Settings -> Secrets and variables -> Actions 新增上述 secret。

## 後續更新流程

1. 本機開發並測試。
2. commit/push 到 `main`、`master` 或 `codex/lightweight-auth-evidence-ui`。
3. GitHub Actions 重新上傳 seed、套用 D1 schema、部署 Worker、部署 Pages。
4. 線上可用 `https://stock-scanner-beta.pages.dev/api/health` 檢查狀態。

## 部署時不做的事

- 不在每次 deploy 時重新全量抓五年官方資料。
- 不把 D1 使用者、session、持股資料清空。
- 不把 R2 快取清空。

## 需要再確認的設定

當正式分支改成 `main` 後，請到 Cloudflare Pages 專案確認 Production branch 是否改為 `main`。目前 CLI 直接部署已可使用；若改採 Cloudflare Dashboard 的 Git integration，這個分支設定會影響哪個 branch 成為 production。
