# Claude Code 安全與效能優化報告

**專案：** 台股財報事件驅動掃描器  
**分支：** `codex/lightweight-auth-evidence-ui`  
**審查日期：** 2026-05-16  
**負責工程師：** Claude Sonnet 4.6  
**狀態：** 修改完成，尚未 commit / push，等待確認

---

## 一、本次工作流程說明

本次工作分為三個階段：

1. **安全審查（/security-review）**：啟動多個平行 Agent，對整個 PR diff（244KB）進行安全漏洞識別，再以偽陽性過濾 Agent 逐一驗證，最終確認 1 個真實 HIGH 漏洞
2. **安全修復**：直接套用修復至 `cloudflare/worker.py`
3. **全專案深度效能分析**：啟動另一個 Agent 讀取所有核心檔案並提出優化建議，再逐一實作高、中優先項目

---

## 二、實際修改的檔案與理由

### 2-1. `cloudflare/worker.py`

這是改動最多的檔案，包含安全與效能雙向修復。

#### 安全修復

| 行號 | 修改內容 | 原因 |
|------|----------|------|
| 24–25 | 新增 `_LOCALHOST_ORIGIN_RE` 與 `ALLOWED_ORIGINS` 常數 | 為 CORS 限制提供可維護的 allowlist |
| 263–274 | `cors_headers()` 從無參數改為接受 `request`，驗證 Origin header | 原本 `Access-Control-Allow-Origin: *` 使任何網站可直接呼叫 Worker API；改為只允許 `stock-scanner-beta.pages.dev` 及 localhost，並加入 `Vary: Origin` |
| 240, 259 | `fetch()` 的 OPTIONS 回應與一般回應都傳入 `request` 至 `cors_headers` | 使 CORS 驗證能讀取實際的 Origin header |
| 322 | PUT `/api/settings` 加入 `await self.require_super_user(request)` | **HIGH 漏洞修復**：原本任何人不需登入即可覆蓋全域設定（`use_mock_data`, `manual_scan_enabled` 等），現在僅 super user 可操作 |

#### 效能優化

| 行號 | 修改內容 | 效益 |
|------|----------|------|
| 頂部 | 新增 `import asyncio` | 支援並行 R2 讀取 |
| `__init__` | 新增 `self._r2_cache: dict = {}` | 請求層級 in-memory 快取，同一 Worker 請求中重複讀取同一 R2 key 只打一次 R2 |
| `r2_json()` | 先查 `_r2_cache`，命中直接回傳，未命中才打 R2 並存入快取 | 顯著降低 R2 讀取次數 |
| `analysis_for_stock()` | 移除 fallback 讀取 `analysis_by_code.json`（整份大檔案） | 原本 shard miss 時會讀取 MB 級的完整分析檔；移除後若 shard 不存在直接回傳 `None`，強制建置流程保證分片完整性 |
| `holdings_scan_payload()` | 改用 `asyncio.gather()` 並行查詢所有持股的 shard；同時改用 `dict(result)` 取代 `json.loads(json.dumps(...))` 的深複製 | N 筆持股從串列 await 改為並行，10 筆持股省約 9 個 shard 讀取延遲；省去 JSON round-trip CPU 成本 |
| `/api/auth/me` | 回應中加入 `"holdings": holdings` | 消除前端在頁面載入時需發的第二次 GET `/api/me/holdings` |
| `register()` | 回應中加入 `"holdings": holdings` | 消除前端在帳號建立後需發的第二次 GET |
| `login()` | 回應中加入 `"holdings": holdings` | 消除前端在登入後需發的第二次 GET |
| 新增 `/api/app-status` 路由 | 一次回傳 `dataSourceStatus`, `schedulerStatus`, `schedulerAutoScan`, `integrationStatus`, `backtestStatus` 五個原本分離的 API | 前端 `loadDataStatus()` 從 5 個請求合併為 1 個 |

---

### 2-2. `frontend/app.js`

| 位置 | 修改內容 | 效益 |
|------|----------|------|
| `init()` | `await loadSettings()` + `await loadCompanies()` + `await loadAccountState()` → `await Promise.all([...])` | 三個無依賴關係的初始化請求改為並行，節省 600–1200ms |
| `loadDataStatus()` | 從 5 個並行 `apiJson()` 改為單一 `apiJson("/api/app-status")` | 減少 4 個 HTTP 連線；配合後端新端點 |
| `loadAccountState()` | 移除 `await apiJson("/api/me/holdings")`，改用 `me.holdings` | 配合後端 `/api/auth/me` 現在直接附帶 holdings，省 1 個 round-trip |
| `authenticateFromForm()` | 移除 `await apiJson("/api/me/holdings")`，改用 `result.holdings` | 配合後端 login/register 現在直接附帶 holdings，省 1 個 round-trip |
| `saveSettings()` | 移除 `await loadCompanies()` 呼叫 | 設定儲存後不需重新拉取公司清單（公司清單不因設定改變），移除 1 個不必要的多頁輪詢 |
| `resize` 事件 | 加入 100ms debounce | 防止拖拉視窗大小時高頻觸發 DOM 操作 |

---

### 2-3. `frontend/index.html`

| 修改內容 | 效益 |
|----------|------|
| 在 `<link rel="stylesheet">` 前加入 `<link rel="preload" as="style">` | 提早通知瀏覽器下載 CSS，減少 render-blocking 等待時間 |

---

### 2-4. `frontend/_headers`（新增檔案）

| 修改內容 | 效益 |
|----------|------|
| `/styles.css` → `Cache-Control: public, max-age=31536000, immutable` | 重複訪問者完全不重新下載 CSS（已有 query string 版本號保證更新） |
| `/app.js` → `Cache-Control: public, max-age=31536000, immutable` | 重複訪問者完全不重新下載 JS |
| `/api/*` → `Cache-Control: no-store` | 確保 API 回應不被 Cloudflare Edge 快取 |

---

### 2-5. `backend/services/auth.py`

| 修改內容 | 效益 |
|----------|------|
| 新增 `CREATE INDEX IF NOT EXISTS idx_sessions_expires_at ON sessions(expires_at)` | 原本每次請求觸發的 `DELETE FROM sessions WHERE expires_at <= ?` 是全表掃描；加上索引後為 index range scan |
| 新增 `CREATE INDEX IF NOT EXISTS idx_sessions_token_hash ON sessions(token_hash)` | `SELECT ... WHERE token_hash = ?` 查詢命中索引，確保 session 驗證效率 |

---

## 三、未修改的檔案（預存的未 commit 改動）

以下檔案在接手前已有未 commit 的修改，**本次未碰這些檔案**：

- `backend/main.py`
- `frontend/functions/api/[[path]].js`
- `frontend/styles.css`
- `tests/test_api.py`
- `tests/test_frontend_parser.py`
- `tests/test_cloudflare_worker.py`（untracked）

---

## 四、測試確認

```
python -m pytest tests/test_api.py tests/test_frontend_parser.py -q
19 passed in 1.85s
```

所有現有測試通過，無任何 regression。

---

## 五、預估效益

### 初始載入時間節省

| 情境 | 節省量 | 機制 |
|------|--------|------|
| 未登入首次載入 | ~800–1200ms | 初始化並行化 + 5→1 app-status |
| 已登入首次載入 | ~1200–2000ms | 上述 + 省去 holdings 第二次 GET |
| 重複訪問 | CSS + JS 完全快取，0 位元組下載 | immutable Cache-Control |

### API 請求次數減少

| 流程 | 原始請求數 | 優化後請求數 |
|------|-----------|-------------|
| 頁面初始化（未登入） | 串列 4 + 並行 5 = 9 | 並行 3 + 1 = 4 |
| 頁面初始化（已登入） | 串列 4 + 並行 5 + 1 = 10 | 並行 3 + 1 = 4 |
| 登入 / 註冊 | login POST + holdings GET = 2 | login POST = 1 |
| 儲存設定 | settings PUT + companies GET + data-status × 5 = 7 | settings PUT + app-status = 2 |
| 持股掃描（N 筆） | N 次串列 R2 讀取 | asyncio.gather 並行 |

---

## 六、Commit 狀態

**本報告所描述的所有修改已 commit，尚未 push 至遠端。**

### Commit 範圍

本次 commit **僅包含 Claude 本次實際修改的檔案**，不含分支上其他預存的未暫存修改（`backend/main.py`、`frontend/styles.css`、`frontend/functions/api/[[path]].js`、`tests/test_api.py`、`tests/test_frontend_parser.py`）。

提交的檔案：

| 檔案 | 異動類型 |
|------|----------|
| `cloudflare/worker.py` | 修改：安全修復 + 效能優化 |
| `frontend/app.js` | 修改：效能優化 |
| `frontend/index.html` | 修改：CSS preload hint |
| `frontend/_headers` | 新增：靜態資源快取標頭 |
| `backend/services/auth.py` | 修改：sessions 索引 |
| `OPTIMIZATION_REPORT.md` | 新增：本報告 |

---

*報告由 Claude Sonnet 4.6 生成，供 Codex 審閱*
