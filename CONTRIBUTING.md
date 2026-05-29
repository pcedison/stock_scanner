# 貢獻指南 / Contributing

歡迎為 Stock Scanner 貢獻！本文件說明開發環境、約定與送出 Pull Request 的流程。送出貢獻即表示你同意依本專案的 [Apache License 2.0](./LICENSE) 授權你的貢獻。

## 開發環境 / Getting Started

建議使用 Python 3.12。Cloudflare seed zip 以 Git LFS 追蹤，請先安裝並啟用 `git lfs`。

```bash
git lfs install            # 首次安裝 git-lfs 後執行一次
python -m pip install --upgrade pip
python -m pip install -r requirements.txt -c constraints.txt
npm ci
python -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
```

開啟 <http://127.0.0.1:8000>。Windows 使用者也可執行 `./start_windows.ps1`。

本機 FastAPI 設定預設寫入 `data/settings.local.json`（已 gitignore）；`data/settings.example.json` 為提交於版控的預設樣板。完整架構以 [`docs/current_architecture.md`](./docs/current_architecture.md) 為準。

## 開發約定 / Conventions

- **API contract 雙向同步**：新增或調整 API 行為時，需同時考量 FastAPI（`backend/`）與 Cloudflare Worker（`cloudflare/worker.py`）兩端的 contract，並更新 `tests/test_api_worker_contracts.py`。
- **Cloudflare schema 變更**：請新增版本化 migration `cloudflare/migrations/*.sql`，**不要**只改 `cloudflare/schema.sql`。
- **前端 DOM 安全**：新的 renderer 應優先使用 `frontend/dom.js` 中的 escape / DOM helper，避免新增 ad hoc `innerHTML`，以維持 XSS 防護的一致性。
- **相依套件管理**：Python 套件透過 `requirements.txt` + `constraints.txt` 安裝；`constraints.txt` 釘住 direct 與 transitive 相依（由 `pip-compile` 產生）。新增套件時請一併更新兩個檔案。
- **Python 風格與型別**：使用 `ruff`（lint + format）與 `mypy`，設定見 [`pyproject.toml`](./pyproject.toml)；開發工具版本釘於 [`requirements-dev.txt`](./requirements-dev.txt)。CI 會擋 `ruff check .` 與 `mypy backend`。整個 `backend` 已通過 mypy 型別檢查（無 per-module override），新增程式碼請維持此標準。
- **前端 JS 風格**：使用 `eslint`（設定見 [`eslint.config.mjs`](./eslint.config.mjs)），CI 會擋 `npm run lint`。`prettier` 為**選用**格式化（`npm run format`），與 Python 的 `ruff format` 一致地**未納入 CI gate**，以避免一次性大量重排既有碼。請至少對你改動的檔案套用 `prettier`。

## 提交前驗證 / Pre-submit Checks

請在本機跑過下列檢查（對應 CI 的 quality gate）：

```bash
# 開發工具（一次安裝）
python -m pip install -r requirements-dev.txt

# 程式風格與型別（對應 CI gate）
npm run lint          # eslint（前端 JS）
python -m ruff check .
python -m mypy backend
# 選用：套用格式（皆未納入 CI gate，全面導入待獨立排版 PR）
npm run format        # prettier（前端 JS）
python -m ruff format .

# 測試與安全
python -m pytest -q
python scripts/check_frontend_hygiene.py
python scripts/check_operational_readiness.py
python -m pip check
npm audit --audit-level=low
python -m bandit -q -r backend cloudflare scripts -x .tmp,.venv,node_modules,tests --severity-level medium

# 部署與前端 e2e（視變更範圍）
python scripts/check_deployment_preflight.py
python scripts/run_wrangler_dev_smoke.py
npm run test:e2e
```

> `ruff` / `mypy` 由 `requirements-dev.txt` 釘住版本，請以 `python -m pip install -r requirements-dev.txt` 安裝，確保與 CI 結果一致。

## Pull Request 流程 / Pull Request Process

1. 從最新的 `main` 開分支（建議命名：`feat/...`、`fix/...`、`chore/...`、`docs/...`）。
2. 保持 commit 聚焦、訊息清楚；一個 PR 處理一件事。
3. 確認上述驗證在本機通過，且 CI（`.github/workflows/ci.yml`、CodeQL）為綠燈。
4. 在 PR 描述中說明動機、做法與測試方式；若有 API/schema 變更，請註明兩端同步狀況。
5. 等待 review。涉及部署流程（`.github/workflows/*`）或安全邊界的變更會被特別審視。

## 回報問題 / Reporting Issues

- 一般 bug 與功能建議：開 GitHub Issue，並盡量附上重現步驟、預期/實際行為與環境資訊。
- **安全漏洞請勿開公開 Issue**，改循 [SECURITY.md](./SECURITY.md) 的私密管道回報。

謝謝你的貢獻！
