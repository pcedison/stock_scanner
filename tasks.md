# 台股財報事件驅動掃描器 — Tasks

> 版本：v0.1
> 用途：給本地端 Codex / 開發者依序執行的任務清單
> 任務格式：`[ ]` 未完成、`[x]` 已完成、`[~]` 進行中

---

## Phase 0 — 專案初始化

### T-000 建立專案結構

- [x] 使用目前工作目錄作為專案根目錄
- [x] 建立 `frontend/`
- [x] 建立 `backend/`
- [x] 建立 `backend/services/`
- [x] 建立 `backend/adapters/`
- [x] 建立 `backend/models/`
- [x] 建立 `data/`
- [x] 建立 `tests/`
- [x] 建立 `README.md`
- [x] 建立 `spec.md`
- [x] 建立 `tasks.md`

驗收：

- 專案根目錄清楚
- Windows、macOS、Linux 都能閱讀 README 啟動

---

### T-001 建立 Python 開發環境

- [x] 建議 Python 版本固定為 3.11 或 3.12
- [x] 建立 `requirements.txt`
- [x] 加入 FastAPI
- [x] 加入 Uvicorn
- [x] 加入 Pydantic
- [x] 加入 pytest
- [x] 加入 requests 或 httpx
- [x] 加入 APScheduler，若 MVP 先不用可暫緩

驗收：

```powershell
python -m pip install -r requirements.txt
python -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
```

可成功啟動。

---

### T-002 Windows 啟動腳本

- [x] 建立 `start_windows.bat`
- [x] 建立 `start_windows.ps1`
- [x] 腳本內使用 `python -m uvicorn`，不要直接呼叫 `uvicorn`
- [x] 若缺少套件，自動提示 `pip install -r requirements.txt`

驗收：

- 雙擊 `start_windows.bat` 可啟動服務
- 不因 PATH 找不到 `uvicorn`

---

## Phase 1 — 前端 MVP 與本地端持股記憶

### T-010 建立單頁 RWD 介面

- [x] 建立 `frontend/index.html`
- [x] 建立 `frontend/styles.css`
- [x] 建立 `frontend/app.js`
- [x] 實作桌機版雙欄/多欄 layout
- [x] 實作手機版單欄卡片 layout
- [x] 搜尋框、按鈕、卡片在手機不得超出畫面

驗收：

- 桌機寬螢幕可正常顯示
- 手機寬度 375px 可正常操作

---

### T-011 首次使用 Onboarding Modal

- [x] 第一次開啟時顯示「是否有持股」詢問
- [x] 支援輸入多檔股票
- [x] 支援股票代碼、公司名、代碼 + 公司名
- [x] 儲存至 `localStorage`
- [x] 設定 `tw_stock_scanner.onboarding_done.v1 = true`

驗收：

- 第一次開啟一定會詢問
- 使用者可略過
- 使用者輸入後能在「我的持股」看到資料

---

### T-012 修正 undefined 持股 bug

背景：先前線上 demo 輸入 `2357 華碩` 後顯示 `undefined`，這是必要修正項。

- [x] 實作 `parseStockInput(input)`
- [x] 實作 `findCompanyByCodeOrName(query)`
- [x] 找不到股票時不可新增持股
- [x] 找不到股票時顯示「找不到資料」
- [x] 不得渲染 `undefined`

測試案例：

| input | expected |
|---|---|
| `2357` | 2357 華碩 |
| `華碩` | 2357 華碩 |
| `2357 華碩` | 2357 華碩 |
| ` 2357  華碩 ` | 2357 華碩 |
| `2330` | 2330 台積電 |
| `不存在股票` | 不新增，顯示錯誤 |

驗收：

- 所有測試案例通過
- 我的持股不再出現 `undefined`

---

### T-013 localStorage 持股 CRUD

- [x] 新增持股
- [x] 已存持股可進入編輯模式
- [x] 更新持股股數
- [x] 更新平均成本
- [x] 減碼
- [x] 出清
- [x] 刪除持股
- [x] 重新整理後資料仍存在
- [x] localStorage JSON 損壞時自動重建

localStorage key：

```txt
tw_stock_scanner.holdings.v1
```

驗收：

- 新增 2357 華碩 1000 股後重新整理仍存在
- 減碼 500 股後剩 500 股
- 出清後移除該股票

---

### T-014 股票搜尋框

- [x] 支援股票代碼搜尋
- [x] 支援繁體中文公司名搜尋
- [x] 支援即時候選結果
- [x] 點候選結果可查看單檔分析
- [x] 可加入持股

驗收：

- 搜尋 `台積電` 可找到 2330
- 搜尋 `2330` 可找到台積電
- 搜尋 `2357 華碩` 可找到華碩

---

## Phase 2 — Mock Data 與資料模型

### T-020 建立示範公司資料

- [x] 建立 `data/sample_companies.json`
- [x] 至少包含 2330 台積電
- [x] 至少包含 2357 華碩
- [x] 至少包含一檔符合進場條件的示範公司
- [x] 至少包含一檔觸發出場條件的示範公司
- [x] 至少包含一檔金融業示範公司
- [x] 標記 `market`, `industryName`, `isFinancial`

驗收：

- 前端可搜尋所有 sample companies
- 金融業可被排除

---

### T-021 建立財報示範資料

- [x] 建立 `data/sample_fundamentals.json`
- [x] 包含月營收資料
- [x] 包含季 EPS
- [x] 包含季淨利
- [x] 包含 PER
- [x] 包含存貨週轉率
- [x] 包含近 5 年年度淨利

驗收：

- 每檔示範公司能產出 FundamentalSnapshot

---

### T-022 後端資料模型

- [x] 建立 `backend/models/company.py`
- [x] 建立 `backend/models/financial.py`
- [x] 建立 `backend/models/holding.py`
- [x] 建立 `backend/models/analysis.py`
- [x] 使用 Pydantic 定義 schema

驗收：

- pytest 可驗證資料模型建立成功
- invalid data 會被攔截

---

## Phase 3 — 後端 API

### T-030 FastAPI 基礎服務

- [x] 建立 `backend/main.py`
- [x] 提供 `/api/health`
- [x] 掛載 frontend 靜態檔案
- [x] 設定 CORS，MVP 本地端允許 localhost

驗收：

```txt
GET /api/health -> 200 OK
```

---

### T-031 公司搜尋 API

- [x] `GET /api/companies/search?q=...`
- [x] 支援代碼
- [x] 支援中文名稱
- [x] 支援部分比對
- [x] 回傳最多 20 筆候選

驗收：

- `/api/companies/search?q=2357` 回傳華碩
- `/api/companies/search?q=華碩` 回傳華碩

---

### T-032 單檔分析 API

- [x] `POST /api/analyze/{stockCode}`
- [x] 載入 FundamentalSnapshot
- [x] 呼叫規則引擎
- [x] 回傳 AnalysisResult

驗收：

- 2357 可回傳分析結果
- 金融業若排除，回傳 EXCLUDED

---

### T-033 掃描全市場 API

- [x] `POST /api/scan/market`
- [x] 可依設定掃描上市/上櫃
- [x] 可排除金融業
- [x] 產生進場清單
- [x] 產生觀察清單
- [x] 產生排除清單

驗收：

- Mock data 中符合進場條件者出現在進場清單

---

### T-034 掃描持股 API

- [x] `POST /api/scan/holdings`
- [x] 前端傳入 holdings 陣列
- [x] 後端逐檔分析
- [x] 回傳 HOLD / ADD_WATCH / WARNING / EXIT

驗收：

- 持有觸發出場條件的 mock 股票時，回傳 EXIT

---

### T-035 設定 API

- [x] `GET /api/settings`
- [x] `PUT /api/settings`
- [x] 設定可保存於本地 JSON 或 SQLite
- [x] 前端設定開關能讀寫

設定項：

- [x] auto_scan_full_market
- [x] manual_scan_enabled
- [x] exclude_financial_industry
- [x] use_mock_data
- [x] scan_twse
- [x] scan_tpex
- [x] spring_festival_guard

驗收：

- 改設定後重新整理仍存在

---

## Phase 4 — 規則引擎

### T-040 建立 RuleEngine

- [x] 建立 `backend/services/rules.py`
- [x] 建立 `evaluate_entry(snapshot, settings)`
- [x] 建立 `evaluate_holding(snapshot, holding, settings)`
- [x] 每條規則回傳 RuleResult
- [x] AnalysisResult 需包含 reasons

驗收：

- 可清楚看到每檔股票為何 ENTRY / EXIT / HOLD

---

### T-041 進場規則 E1–E6

- [x] E1 近 5 年沒有虧損
- [x] E2 近 3 年淨利正成長
- [x] E3 今年累計營收年增率 >= 50%
- [x] E4 PER < 15
- [x] E5 存貨週轉率 > 2.5
- [x] E6 金融業預設排除

驗收：

- 每條規則都有 unit test
- 符合全部條件才 ENTRY

---

### T-042 出場規則 X1–X5

- [x] X1 月營收年增率 < 30%
- [x] X2 月營收年增率較上月少超過 20 個百分點
- [x] X3 EPS 衰退
- [x] X4 季度 EPS 減少超過 10%
- [x] X5 淨利衰退

驗收：

- X4 觸發時必須高優先 EXIT
- X1/X2 於春節月份需走春節保護

---

### T-043 春節保護規則

- [x] 建立 `is_spring_festival_month(date, market_calendar)`
- [x] 春節當月不因單月營收下降直接 EXIT
- [x] 標記 SPRING_FESTIVAL_WATCH
- [x] 可用 1+2 月合併營收或近 3 個月平均輔助判斷
- [x] Q1 EPS/淨利確認衰退後仍可 EXIT

驗收：

- 春節月 X1/X2 不直接 EXIT
- 非春節月 X1/X2 正常 WARNING/EXIT

---

### T-044 加碼觀察規則

- [x] 原進場條件仍符合
- [x] 累計營收年增率 >= 50%
- [x] EPS 年增率 > 0
- [x] 淨利年增率 > 0
- [x] PER < 15
- [x] 存貨週轉率 > 2.5
- [x] 未觸發出場條件

驗收：

- 僅在條件優於 HOLD 時回傳 ADD_WATCH

---

## Phase 5 — 事件驅動排程

### T-050 市場日曆模型

- [x] 建立 `MarketCalendar`
- [x] 可讀取官方或 mock 市場開休市資料
- [x] 判斷是否交易日
- [x] 判斷下一個交易日

驗收：

- 休市日不輸出「立即操作」訊號

---

### T-051 Wake Up Decision

- [x] 建立 `ScanScheduler.should_wake_up(today)`
- [x] 每月 8–15 日為月營收掃描窗口
- [x] 3 月底年報窗口
- [x] 5 月中 Q1 窗口
- [x] 8 月中 Q2 窗口
- [x] 11 月中 Q3 窗口
- [x] 春節月份啟用特殊窗口

驗收：

- 非關鍵日期回傳 SLEEP
- 關鍵日期回傳對應 ScanEvent

---

### T-052 自動掃描開關

- [ ] 若 `auto_scan_full_market = true`，關鍵日期自動掃描市場
- [ ] 若 `auto_scan_full_market = false`，只提醒可手動掃描
- [ ] 若 `manual_scan_enabled = false`，前端停用手動掃描按鈕

驗收：

- 設定開關能影響掃描行為

---

## Phase 6 — 官方資料源串接

### T-060 TWSE 月營收 Adapter

- [x] 建立 `TwseDataProvider`
- [x] 串接 TWSE OpenAPI 上市公司每月營業收入彙總表
- [x] 轉成 MonthlyRevenue
- [x] 處理民國年/西元年轉換
- [x] 處理數字逗號與空值

驗收：

- 可取得上市公司月營收資料
- 可轉為系統內部格式

---

### T-061 TPEx 月營收 Adapter

- [x] 建立 `TpexDataProvider`
- [x] 串接 TPEx OpenAPI 上櫃公司每月營業收入彙總表
- [x] 轉成 MonthlyRevenue

### T-061A 官方上市櫃 Universe Provider

- [x] 串接 TWSE 上市公司基本資料 OpenAPI
- [x] 串接 TPEx 上櫃公司基本資料 OpenAPI
- [x] 合併官方公司清單與月營收資料
- [x] 關閉 Mock Data 後改掃官方 TWSE / TPEx universe
- [x] 季報、年報、PER、存貨週轉率不足時回傳 `INSUFFICIENT_DATA`

驗收：

- 可取得上櫃公司月營收資料
- 可掃描官方上市櫃月營收 universe，不再只掃 sample JSON

---

### T-062 財報/季報/年報 Adapter

- [ ] 調查公開資訊觀測站/官方資料下載方式
- [ ] 建立可取得 EPS、淨利、毛利率、存貨週轉率的資料流程
- [ ] 若官方 API 不穩定，先建立手動匯入 CSV 流程
- [ ] 轉成 QuarterlyFinancial / AnnualFinancial

驗收：

- 至少可用 CSV 匯入完成真實財報欄位
- 規則引擎不依賴外部原始格式

---

### T-063 TWSE 市場開休市 Adapter

- [x] 串接或匯入 TWSE 市場開休市日期
- [x] 轉成 MarketCalendar
- [ ] 每年可更新

驗收：

- 可以判斷 2026 年春節休市區間
- 可以判斷下一個交易日

---

## Phase 7 — 報告與使用者操作

### T-070 進場清單 UI

- [ ] 顯示 ENTRY 清單
- [ ] 顯示 WATCH 清單
- [ ] 顯示排除原因
- [ ] 每檔股票顯示規則通過/失敗
- [ ] 可加入持股

驗收：

- 使用者能理解為何推薦

---

### T-071 持股追蹤 UI

- [ ] 顯示每檔持股狀態
- [ ] HOLD 顯示續抱原因
- [ ] ADD_WATCH 顯示加碼觀察原因
- [ ] WARNING 顯示警戒原因
- [ ] EXIT 顯示出場原因
- [ ] EXIT 卡片提供「採用出場建議」按鈕

驗收：

- 按「採用出場建議」後，持股被清除或減碼

---

### T-072 匯出報告

- [ ] 產生 Markdown 報告
- [ ] 產生 CSV 報告
- [ ] 報告包含掃描時間、資料來源、規則結果

驗收：

- 可下載報告

---

## Phase 8 — 測試

### T-080 Unit Tests

- [x] parseStockInput
- [x] company search
- [ ] localStorage schema helper，若前端有測試框架
- [x] E1–E6
- [x] X1–X5
- [x] spring festival guard
- [x] financial exclusion
- [x] wake up scheduler

驗收：

```bash
pytest
```

通過。

---

### T-081 Integration Tests

- [x] MockDataProvider → scan market
- [x] MockDataProvider → scan holdings
- [x] API search
- [x] API analyze
- [x] API settings

驗收：

- API 測試全部通過

---

### T-082 Manual QA Checklist

- [ ] 第一次開啟會問持股
- [x] 輸入任意股票代碼或公司名稱不會 undefined
- [ ] 可新增第二檔股票
- [ ] 可減碼
- [ ] 可出清
- [x] 掃描全市場有結果
- [x] 掃描持股有結果
- [x] 手機版可操作
- [ ] 關掉瀏覽器重開後 localStorage 還在

---

## Phase 9 — 部署與文件

### T-090 README

- [x] 說明專案用途
- [x] 說明投資風險
- [x] 說明 Windows 啟動方法
- [x] 說明 macOS/Linux 啟動方法
- [x] 說明資料源狀態
- [x] 說明 mock data 與真實資料差異

驗收：

- 新開發者照 README 能啟動

---

### T-091 Codex 開發提示詞

將以下提示詞提供給 Codex：

```txt
你正在開發「台股財報事件驅動掃描器」。請先閱讀 spec.md 與 tasks.md。
請依 tasks.md 順序開發，不要跳過驗收條件。
優先完成 MVP：前端 RWD、首次持股輸入、localStorage 持股管理、搜尋框、MockDataProvider、進場/出場規則、掃描全市場、掃描持股、設定開關。
請特別修正：輸入「2357 華碩」後不得出現 undefined。
所有投資判斷必須輸出原因，不得只給買賣結論。
```

---

## Phase 10 — 後續 Backlog

### B-001 回測系統

- [ ] 匯入歷史月營收
- [ ] 匯入歷史季報
- [ ] 模擬進出場
- [ ] 計算勝率、最大回撤、年化報酬

### B-002 通知系統

- [ ] LINE Notify 或 LINE Messaging API
- [ ] Telegram Bot
- [ ] Email
- [ ] 桌面通知

### B-003 券商同步

- [ ] 調查券商 API
- [ ] 同步真實持股
- [ ] 賣出後自動清除本地持股

### B-004 金融業專用策略

- [ ] 金融業不看存貨週轉率
- [ ] 改看 ROE、逾放比、資本適足率、利差、股利政策等
- [ ] 與主策略分離

### B-005 AI 財報摘要

- [ ] 摘要年報/季報文字
- [ ] 摘要法說會
- [ ] 摘要重大訊息
- [ ] 自動補充風險說明

---

## 任務優先順序總結

最優先：

1. T-012 修正 undefined bug
2. T-013 localStorage 持股 CRUD
3. T-014 搜尋框
4. T-040 規則引擎
5. T-041 進場規則
6. T-042 出場規則
7. T-033 掃描全市場
8. T-034 掃描持股
9. T-010 RWD 介面
10. T-090 README

完成以上後，就是可用 MVP。
