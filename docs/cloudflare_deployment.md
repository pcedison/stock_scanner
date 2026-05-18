# Cloudflare 部署須知

目前部署目標為 Cloudflare Pages + Python Worker + D1 + R2。

## 資源

- Pages project: `stock-scanner-beta`
- Production URL: `https://stock-scanner-beta.pages.dev`
- Worker name: `stock-scanner-beta-api`
- Worker URL: `https://stock-scanner-beta-api.pcedison.workers.dev`
- D1 database: `stock-scanner-beta-db`
- R2 bucket: `stock-scanner-beta-cache`

Pages 透過 `frontend/functions/api/[[path]].js` 將 `/api/*` 代理到 Worker；前端仍呼叫同源 `/api`，所以本機 FastAPI 與 Cloudflare Worker 可以共用大部分前端流程。

## 必要設定

GitHub Actions secrets：

- `CLOUDFLARE_ACCOUNT_ID`
- `CLOUDFLARE_API_TOKEN`

GitHub Actions repository variables：

- `CF_WORKER_HEALTH_URL`，必須是 deployed Worker 的 HTTPS `/api/health` URL。

GitHub environment：

- 建議建立 `production` environment，並在 GitHub repo settings 裡加上 required reviewers 或其他保護規則。

Worker production CORS 在 `cloudflare/wrangler.toml` 的 `[vars]` 管理：

- `APP_ENV = "production"`
- `APP_CORS_ALLOW_ORIGINS = "https://stock-scanner-beta.pages.dev"`

Production 不應放行 localhost、127.0.0.1 或非 HTTPS origin。

## 部署流程

`.github/workflows/cloudflare-deploy.yml` 只在 `main`、`master`、schedule 或手動觸發時部署。一般 feature branch 只跑 validation。

部署順序：

1. 安裝 Python / Node dependencies。
2. 執行 pytest、frontend hygiene、pip check、npm audit。
3. 執行 deployment preflight，確認 CORS、environment gate、concurrency、D1 migration、health check、rollback 等 guardrails 存在。
4. 執行 Playwright browser smoke。
5. 驗證 committed seed zip，並輸出 seed quality summary。
6. 用 offline mode 從 committed zip 重建 `cloudflare/seed/*`。
7. 執行 Worker dry-run，檢查 Cloudflare Python Worker bundle 邊界。
8. 上傳 seed payload 到 R2。
9. 部署前匯出 D1 backup artifact。
10. 套用 `cloudflare/migrations/` 中尚未執行的 D1 migrations。
11. 部署 Worker 與 Pages。
12. 對 `CF_WORKER_HEALTH_URL` 執行 `/api/health` 檢查，並比對 manifest counts。
13. 若 post-deploy health 驗證失敗，CI 會以 `wrangler rollback --yes` 回復 Worker。

D1 rollback 不做自動破壞性還原；部署前的 D1 export 會保留為 artifact，必要時由 operator 下載後人工復原指定資料。

## Seed refresh

`.github/workflows/refresh-cloudflare-seed.yml` 每週會：

1. 使用 official source online mode 重建 seed。
2. 重新封裝 `data/official_cache_seed_2026-05-14.zip` 與 `.sha256`。
3. 重新產生缺漏公司報告。
4. 驗證 seed quality gates。
5. 若內容有變更，開啟或更新 refresh PR。

這讓 production deploy 可以穩定使用 committed offline seed，同時避免資料長期 stale。

## Health monitoring

`.github/workflows/cloudflare-health-monitor.yml` 每 30 分鐘打一次 `CF_WORKER_HEALTH_URL`。目前以 GitHub Actions failure notification 作為基礎告警；若需要更完整的外部監控，可以把同一個 `/api/health` 端點接到 Cloudflare notification、Better Stack、UptimeRobot 或其他監控服務。

## 本機驗證指令

```powershell
python -m pytest -q
python scripts\check_frontend_hygiene.py
python scripts\check_deployment_preflight.py
npx wrangler deploy --config cloudflare\wrangler.toml --dry-run --outdir .tmp\worker-dry-run
python scripts\run_wrangler_dev_smoke.py
npm run test:e2e
```
