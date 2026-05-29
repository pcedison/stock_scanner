# Stock Scanner

[![Validate](https://github.com/pcedison/stock_scanner/actions/workflows/ci.yml/badge.svg)](https://github.com/pcedison/stock_scanner/actions/workflows/ci.yml)
[![CodeQL](https://github.com/pcedison/stock_scanner/actions/workflows/codeql.yml/badge.svg)](https://github.com/pcedison/stock_scanner/actions/workflows/codeql.yml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](./LICENSE)

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

建議 Python 3.12。本專案的 Cloudflare seed zip（`data/official_cache_seed_*.zip`）以 **Git LFS** 追蹤，clone 前請先安裝並啟用：

```bash
git lfs install
# 既有 clone 補拉 LFS 物件：
git lfs pull
```

未安裝 git-lfs 時，新版 seed zip 會以 pointer 檔形式出現，導致 seed 驗證/重建步驟失敗。

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

Local FastAPI settings are written to `data/settings.local.json` by default (gitignored). `data/settings.example.json` is the committed default shape; set `SETTINGS_PATH` when an isolated settings file is needed.

## 驗證

```powershell
python -m pytest -q
python scripts\check_frontend_hygiene.py
python scripts\check_operational_readiness.py
python scripts\check_deployment_preflight.py
python scripts\run_wrangler_dev_smoke.py
python -m pip check
npm audit --audit-level=low
npm run test:e2e
```

Cloudflare seed 驗證：

```powershell
$seedZip = python scripts\seed_utils.py data --print
python scripts\validate_cloudflare_seed_inputs.py --zip $seedZip --summary-md .tmp\seed-quality.md --summary-json .tmp\seed-quality.json --max-age-days 45
$env:CLOUDFLARE_SEED_MODE='offline'; python scripts\build_cloudflare_seed.py; Remove-Item Env:\CLOUDFLARE_SEED_MODE
```

## Cloudflare 部署

部署流程在 `.github/workflows/cloudflare-deploy.yml`：

- production environment gate 與 concurrency。
- R2 seed 上傳前先離線重建與驗證。
- Worker deploy 前先 `wrangler deploy --dry-run`。
- D1 使用 `cloudflare/migrations/` 版本化 migration，部署前匯出 D1 backup artifact。
- Worker/Pages deploy 後檢查 `/api/health` 與 manifest counts。
- deploy 後執行 remote public smoke，驗證 `/api/health`、`/api/app-status`、`/api/data-sources/status`。
- post-deploy 驗證失敗時自動執行 Worker rollback。

必要 GitHub secret / variable：

- `CLOUDFLARE_ACCOUNT_ID`
- `CLOUDFLARE_API_TOKEN`
- repository variable `CF_WORKER_HEALTH_URL`，必須指向 deployed Worker 的 `/api/health` HTTPS URL。

更多部署細節見 `docs/cloudflare_deployment.md`。

Recovery runbook 可用非破壞性方式產生：

```powershell
python scripts\plan_cloudflare_recovery.py --output .tmp\cloudflare-recovery.md
```

Release synchronization gate:

```powershell
python scripts\check_release_sync.py
python scripts\check_release_sync.py --cancel-stale-deploys
python scripts\check_release_sync.py --check-production-health --check-production-smoke --health-url https://example.workers.dev/api/health
python scripts\write_agent_handoff.py --repo pcedison/stock_scanner
```

The default run is a dry run that only reports JSON. `--cancel-stale-deploys` only cancels stale `Deploy to Cloudflare` runs on `main` whose `headSha` is not `origin/main`; it never approves production deploys.
Production health/smoke checks reject stale cache data older than 36 hours and offline-seed production payloads by default. See `docs/agent_handoff.md` for the Codex/Claude shared-state protocol.

## 資料更新

目前 committed seed 由 `python scripts\seed_utils.py data --print` 解析最新 dated seed zip。`.github/workflows/refresh-cloudflare-seed.yml` 會定期從官方來源重建 seed、封裝 zip、驗證品質、產生缺漏公司摘要，並開 PR 更新 committed seed。

缺漏與人工補資料追蹤：

- `data/official_history_failed_companies_2026Q1.csv`
- `docs/official_history_failed_companies_2026Q1.md`

## 開發約定

- 新 API 行為需要同步考慮 FastAPI 與 Worker contract。
- 新 Cloudflare schema 變更應新增 `cloudflare/migrations/*.sql`，不要只改 `cloudflare/schema.sql`。
- 新前端 renderer 應優先使用 `frontend/dom.js` 中的 escape/DOM helper，避免增加 ad hoc `innerHTML`。
- Python dependency 透過 `requirements.txt` + `constraints.txt` 安裝；`constraints.txt` 釘住 direct 與 transitive dependencies。

## 專案資訊 / Project

- 授權：[Apache-2.0](./LICENSE)
- 貢獻指南：[CONTRIBUTING.md](./CONTRIBUTING.md)
- 行為準則：[CODE_OF_CONDUCT.md](./CODE_OF_CONDUCT.md)
- 安全政策：[SECURITY.md](./SECURITY.md)
- 變更紀錄：[CHANGELOG.md](./CHANGELOG.md)
