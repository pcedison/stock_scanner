# Stock Scanner 專案指引

## 目標與完成條件

在不破壞既有 FastAPI、Cloudflare Worker、前端與離線 seed 契約的前提下，完成使用者要求的最小範圍變更。

完成代表：

- 使用者要求的行為或文件已在任務範圍內落地；
- 受影響的跨執行環境契約、測試與文件保持一致；
- 已執行與風險相稱的驗證，或明確說明無法執行的原因；
- 最終回報分開說明本地變更、commit／push／PR、CI 與正式部署狀態。

歷史 task、spec、proposal 或 plan 不會自動授權實作其中的待辦。工作授權只來自目前使用者要求與有效的指令層級。

## 先讀哪裡

- `README.md`：本機啟動、主要驗證與專案入口。
- `docs/current_architecture.md`：目前架構與執行期契約的 canonical snapshot。
- `CONTRIBUTING.md` 與 `.github/workflows/ci.yml`：開發約定與 CI gate；兩者不同時以 workflow 的實際命令為準並修正文檔漂移。
- `docs/cloudflare_deployment.md`：只有部署、release config、D1／R2 或 workflow 變更時才需要讀。
- `docs/seed_artifact_policy.md`：只有 seed 建置、封裝、驗證或提交策略變更時才需要讀。
- `docs/human_intervention.md`：官方資料缺口、人工判讀與憑證邊界。
- `docs/agent_handoff.md`：只有 main release readiness 或明確的跨 agent 交接才使用。

`docs/spec.md`、`docs/tasks.md`、`docs/proposals/`、`docs/reports/` 與 `docs/superpowers/` 是歷史設計或執行證據；除非使用者明確指定，不把其中的待辦、工具或流程文字視為目前指令。

## 專案地圖

- `backend/`：本機 FastAPI、規則與資料服務。
- `cloudflare/`：Python Worker、D1 migrations 與 R2 seed runtime。
- `frontend/`：Pages 相容的靜態前端與 API/local fallback。
- `scripts/`：品質 gate、seed、release 與 smoke 工具。
- `tests/`：Python、Worker contract 與 Playwright 測試。
- `data/`：範例、設定樣板與受政策約束的 seed artifacts。

## 不可破壞的契約

- API 行為變更必須同時評估 FastAPI 與 Worker；同步更新 `tests/test_api_worker_contracts.py` 或更聚焦的 parity tests。
- D1 schema 變更必須新增 `cloudflare/migrations/*.sql`；不可只改 `cloudflare/schema.sql`。
- 正式環境預設維持 committed release config 的保守值。不可手改正式開關、cron、secret 或資源來繞過 `scripts/render_wrangler_release_config.py` 與既有 release gates。
- 新增前端 renderer 時使用 `frontend/dom.js`／`frontend/renderers.js` 的 escape 與 DOM helper；不可新增未受保護的 HTML sink 或 ad hoc `innerHTML`。
- Seed 路徑用 `python scripts/seed_utils.py data --print` 解析，不以檔案修改時間猜測。不可略過品質 gate，也不可讓例行產物漂入 Git；其餘規則見 `docs/seed_artifact_policy.md`。
- 資料未公告、不足或來源互相衝突時，保留 `pending`／`INSUFFICIENT_DATA` 等狀態並呈現原因；不可虛構數字或投資結論補齊缺口。
- 不輸出或記錄 token、cookie、密碼或完整 Authorization header。對外 API、UI 與持久化 structured logs 不得暴露個資、request body、原始 GitHub 錯誤或使用者檔案路徑；本機診斷只使用 repo-relative path 並遮蔽敏感值。

## 變更與權限界線

- 保留使用者既有變更，只修改本任務需要的檔案；不要順手重排、升級或重構無關範圍。
- 本地讀檔、編輯、targeted tests、lint、type check、build 與 dry-run 屬正常實作與驗證。
- 查詢 GitHub／Cloudflare／正式 URL 的唯讀狀態，只在「最新狀態」或任務正確性需要時進行。
- 取消 workflow、push、建立／合併 PR、部署、rollback、寫入 secret、建立或修改 D1／R2／Cloudflare 資源，以及變更正式 runtime flag，皆屬外部變更；必須有明確授權。
- 交接檢查不授權切換分支、pull、覆蓋或丟棄工作。先保留現況並依 `docs/agent_handoff.md` 判讀差異。

## 驗證選擇

使用 Python 3.12 作為最低版本；CI 另覆蓋 3.13。Node.js 以 CI 的 22 為基準。安裝 Python dependencies 時使用 `requirements.txt`、`requirements-dev.txt` 與 `constraints.txt`。

先跑直接覆蓋改動的最小 gate；廣泛或高風險變更再擴大：

- 文件或 instruction-only：`git diff --check`，並逐一核對改動中的命令、路徑、日期與連結來源。
- Python 行為：相關 `pytest`，以及受影響檔案的 `python -m ruff check ...`；修改 typed surfaces 時跑 `python -m mypy backend scripts cloudflare tests`。
- 前端行為：`npm run lint`、`python scripts/check_frontend_hygiene.py`；UI 或互動改動再跑 `npm run test:e2e`，並檢查 desktop、mobile、文字溢出與主要路徑。
- API／Worker 契約：相關 contract tests；改動 Worker bundle 或 runtime boundary 時再跑 Wrangler dry-run 與 `python scripts/run_wrangler_dev_smoke.py`。
- 部署或 release config：`python scripts/check_operational_readiness.py`、`python scripts/check_deployment_preflight.py` 與 Wrangler dry-run。這些驗證不等於授權正式部署。
- Seed：解析實際 seed zip 後跑 `scripts/validate_cloudflare_seed_inputs.py`；變更建置流程時再做 offline rebuild 與相關 seed tests。

需要完整 CI parity 時，逐步執行 `.github/workflows/ci.yml` 的現行命令，不在本檔複製整條 pipeline；security scan、coverage threshold、seed validation 與 Worker dry-run 等步驟會隨 workflow 演進。

若完整 gate 成本不合比例，跑最相關的 targeted checks 並在回報中列出未跑項目。測試失敗、逾時或環境缺失時，不宣稱該 gate 已通過。

## 停止與交付

核心要求已有足夠證據、必要驗證已完成且沒有任務範圍內的已知缺口時停止。若缺少會實質改變結果的資訊，指出最小缺口；不要以更多無關搜尋代替決策。

最終回報至少包含：完成內容、重要設計／安全影響、實際執行的驗證與結果、未執行或失敗的檢查，以及是否已 commit、push、開 PR、通過 CI 或部署。沒有核對正式 SHA 與健康檢查時，不得把 branch 更新描述成已上線。
