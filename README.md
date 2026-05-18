# Stock Scanner

台股掃描與持股分析工具。專案已從早期純前端 MVP 進化為 server-backed、Cloudflare-ready 架構：本機使用 FastAPI，部署端使用 Cloudflare Pages + Python Worker + D1 + R2。

完整現況以 `docs/current_architecture.md` 為準；舊版規格中的 localStorage 描述只代表早期 MVP 設計。現在持股、設定、session 以 API 儲存為主，前端 local fallback 只用於離線或 API 不可用時的降級情境。

## 主要能力

- 台股全市場與持股掃描，包含進場、觀察、排除規則。
- 官方資料來源整合：TWSE、TPEx、MOPS 財報與營收資料。
- FastAPI 與 Cloudflare Worker 共享 API contract 測試。
- Cloudflare seed 可離線重建、驗證、封裝，部署不依賴即時外部 API。
- Worker `/api/health` 回報 cache counts 與資料品質狀態。
- GitHub Actions 覆蓋 Python tests、Playwright browser smoke、Worker dry-run、wrangler dev smoke、seed quality、deployment preflight。

## 本機啟動

建議 Python 3.12。

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements.txt -c constraints.txt
npm ci
python -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
```

打開：

```text
http://127.0.0.1:8000
```

Windows 也可以使用：

```powershell
.\start_windows.ps1
```

## 驗證

```powershell
python -m pytest -q
python scripts\check_frontend_hygiene.py
python scripts\check_deployment_preflight.py
python scripts\run_wrangler_dev_smoke.py
python -m pip check
npm audit --audit-level=low
npm run test:e2e
```

Cloudflare seed 驗證：

```powershell
python scripts\validate_cloudflare_seed_inputs.py --zip data\official_cache_seed_2026-05-14.zip --summary-md .tmp\seed-quality.md
$env:CLOUDFLARE_SEED_MODE='offline'; python scripts\build_cloudflare_seed.py; Remove-Item Env:\CLOUDFLARE_SEED_MODE
```

## Cloudflare 部署

部署流程在 `.github/workflows/cloudflare-deploy.yml`：

- production environment gate 與 concurrency。
- R2 seed 上傳前先離線重建與驗證。
- Worker deploy 前先 `wrangler deploy --dry-run`。
- D1 使用 `cloudflare/migrations/` 版本化 migration，部署前匯出 D1 backup artifact。
- Worker/Pages deploy 後檢查 `/api/health` 與 manifest counts。
- post-deploy 驗證失敗時自動執行 Worker rollback。

必要 GitHub secret / variable：

- `CLOUDFLARE_ACCOUNT_ID`
- `CLOUDFLARE_API_TOKEN`
- repository variable `CF_WORKER_HEALTH_URL`，必須指向 deployed Worker 的 `/api/health` HTTPS URL。

更多部署細節見 `docs/cloudflare_deployment.md`。

## 資料更新

目前 committed seed 是 `data/official_cache_seed_2026-05-14.zip`。`.github/workflows/refresh-cloudflare-seed.yml` 會定期從官方來源重建 seed、封裝 zip、驗證品質、產生缺漏公司摘要，並開 PR 更新 committed seed。

缺漏與人工補資料追蹤：

- `data/official_history_failed_companies_2026Q1.csv`
- `docs/official_history_failed_companies_2026Q1.md`

## 開發約定

- 新 API 行為需要同步考慮 FastAPI 與 Worker contract。
- 新 Cloudflare schema 變更應新增 `cloudflare/migrations/*.sql`，不要只改 `cloudflare/schema.sql`。
- 新前端 renderer 應優先使用 `frontend/dom.js` 中的 escape/DOM helper，避免增加 ad hoc `innerHTML`。
- Python dependency 透過 `requirements.txt` + `constraints.txt` 安裝；`constraints.txt` 釘住 direct 與 transitive dependencies。
