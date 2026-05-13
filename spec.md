# 台股財報事件驅動掃描器 — Spec

> 版本：v0.1
> 狀態：給本地端 Codex 開發用的產品與技術規格草案
> 語言：繁體中文
> 投資提醒：本工具只提供依規則產生的研究輔助訊號，不構成投資建議、保證獲利或代客操作。

---

## 1. 專案目標

建立一套「台灣上市、上櫃公司基本面掃描工具」，依照長輩提供的進出場規則，自動或手動掃描市場、月營收、季報、年報，並根據使用者是否持有股票提供不同輸出。

核心精神：

> 找「長期賺錢 + 高成長 + 估值不貴」的公司；當成長轉弱，就紀律出場。

本系統不是看盤軟體，不以即時股價或當沖為主，而是「事件驅動」的基本面掃描器。

---

## 2. 使用者情境

### 2.1 第一次使用

系統第一次啟動時，必須主動詢問使用者目前是否有持股。

使用者可輸入：

- 股票代碼，例如 `2357`
- 繁體中文公司名稱，例如 `華碩`
- 股票代碼 + 公司名稱，例如 `2357 華碩`
- 持股張數或股數
- 平均成本，選填

輸入後資料存在本地端記憶，預設使用瀏覽器 `localStorage`，不主動上傳。

### 2.2 沒有持股

若使用者沒有持股，系統主要輸出：

- 適合進場清單
- 接近進場觀察清單
- 排除原因

### 2.3 有持股

若使用者有持股，系統優先檢查手中持股，輸出：

- 續抱
- 可加碼觀察
- 警戒
- 建議出清離場

使用者按下「採用出場建議」後，系統需從本地端持股中自動減碼或清除該檔股票。

### 2.4 平時使用

長輩補充邏輯：

> 每年看股的日期剩每月 10 日及季報公布日期，其他時間不用理股市。

系統應設計成：

- 平常低頻待命或休眠
- 接近每月 10 日、季報、年報公布時間時 wake up
- 自動或手動執行掃描
- 非關鍵日期仍可手動掃描，但預設不每天主動打擾使用者

---

## 3. 官方資料節奏與事件驅動規則

### 3.1 月營收

上市、上櫃公司每月營運情形通常應於每月 10 日以前公告申報上月份資料。

系統排程建議：

- 每月 8 日：預備 wake up，檢查是否已有公司提前公告
- 每月 10 日：主要掃描日
- 每月 11–15 日：補掃延遲公告與特殊公司
- 若遇國定假日或休市，順延至下一個交易日/工作日掃描

### 3.2 季報

一般公司第一季、第二季、第三季財報通常在各季結束後 45 日內公告申報。

系統排程建議：

- Q1：5 月中旬前後
- Q2 / 半年報：8 月中旬前後
- Q3：11 月中旬前後

### 3.3 年報

年度財報通常在會計年度結束後 3 個月內公告申報。

系統排程建議：

- 隔年 3 月底前後掃描年報
- 年報用於確認近 5 年是否虧損、近 3 年淨利是否正成長

### 3.4 特殊公司與特殊產業

必須保留彈性，不可將所有公司寫死同一申報日期。

特殊處理：

- 金融控股、銀行、證券、保險、期貨等金融業預設排除主策略
- 保險業或具保險業子公司的公開發行公司，可能存在月營收申報延後至每月 15 日以前的情況
- 第一上市、第一上櫃公司可能有不同申報時程
- 未來資料源應能標記特殊公司類型與特殊申報期限

### 3.5 國定假日與交易日

系統不應寫死假日。

需求：

- 每年讀取 TWSE / TPEx 官方市場開休市日曆
- 非交易日不產生「立即操作」訊號
- 若掃描日期落在休市日，延後到下一個交易日或下一個可執行日
- 春節月份須啟動特殊營收判斷

---

## 4. 長輩策略邏輯總整理

### 4.1 策略類型

本策略屬於「基本面成長股策略」。

判斷核心不是短線價格，而是：

1. 公司長期是否穩定賺錢
2. 近期營收是否高速成長
3. 估值是否不貴
4. 庫存週轉是否正常
5. EPS、淨利是否持續改善

---

## 5. 進場條件

一家公司要進入「適合進場清單」，預設必須同時符合以下條件。

| 編號 | 條件 | 預設判斷 |
|---|---|---|
| E1 | 近 5 年沒有虧損 | 最近 5 個年度淨利皆 >= 0 |
| E2 | 近 3 年淨利正成長 | 最近 3 年年度淨利逐年成長 |
| E3 | 今年已公告營收年增率達 50% 以上 | 今年累計營收 YoY >= 50% |
| E4 | 本益比小於 15 | PER < 15 |
| E5 | 存貨週轉率大於 2.5 | Inventory Turnover > 2.5 |
| E6 | 排除金融業 | 金融業預設不納入主策略 |

### 5.1 進場條件白話

公司要長期有賺錢，近年獲利持續變好，而且現在仍處於高成長，股價估值也不能太貴。

### 5.2 營收年增率預設版本

建議預設採用「今年累計營收年增率」作為主判斷。

原因：單月營收容易被春節、出貨遞延、匯率或一次性訂單影響。

可設定選項：

- `revenue_growth_mode = cumulative_ytd`：今年累計營收年增率，預設
- `revenue_growth_mode = monthly`：單月營收年增率
- `revenue_growth_mode = trailing_3m_avg`：近 3 個月平均年增率

---

## 6. 持有期間追蹤

持有股票後，系統不需要每天盯盤，主要追蹤：

| 頻率 | 追蹤項目 |
|---|---|
| 每月 | 月營收、累計營收、營收年增率、年增率是否降溫 |
| 每季 | EPS、淨利、毛利率、營益率、存貨週轉率 |
| 每年 | 近 5 年虧損紀錄、近 3 年淨利成長趨勢 |

持有期間輸出狀態分為：

| 狀態 | 意義 |
|---|---|
| HOLD | 續抱，成長趨勢仍強 |
| ADD_WATCH | 可加碼觀察，成長仍強且估值仍合理 |
| WARNING | 警戒，有部分數據轉弱 |
| EXIT | 建議出清離場，已觸發出場條件 |
| EXCLUDED | 產業或資料不適用，不進入主策略 |

---

## 7. 加碼觀察條件

加碼必須比續抱更嚴格。

預設可加碼觀察條件：

| 編號 | 條件 |
|---|---|
| A1 | 原進場條件仍符合 |
| A2 | 今年累計營收年增率仍 >= 50% |
| A3 | 最新季 EPS 年增率 > 0 |
| A4 | 最新季淨利年增率 > 0 |
| A5 | PER 仍 < 15 |
| A6 | 存貨週轉率未惡化，且仍 > 2.5 |
| A7 | 未觸發任何出場條件 |

加碼只是「觀察建議」，不自動買入。

---

## 8. 出場條件

只要觸發任一核心出場條件，即應至少標記為 WARNING；若確認為基本面轉弱，則標記 EXIT。

| 編號 | 條件 | 預設判斷 |
|---|---|---|
| X1 | 月營收年增率掉到 30% 以下 | monthly_revenue_yoy < 30% |
| X2 | 月營收年增率較上月突然少超過 20 個百分點 | previous_month_revenue_yoy - current_month_revenue_yoy > 20 |
| X3 | 每股獲利 EPS 衰退 | latest_quarter_eps_yoy < 0 |
| X4 | 季度 EPS 減少超過 10% | latest_quarter_eps_yoy <= -10% |
| X5 | 淨利衰退 | latest_quarter_net_income_yoy < 0 或 annual_net_income_yoy < 0 |

### 8.1 出場條件白話

這套方法買的是「成長」。一旦營收、EPS、淨利顯示成長明顯轉弱，就不戀戰。

### 8.2 出場判斷優先級

建議優先級：

1. X4 季度 EPS 減少超過 10% → 高優先 EXIT
2. X5 淨利衰退 → 高優先 EXIT 或 WARNING，依衰退幅度與連續性
3. X1 月營收年增率跌破 30% → WARNING 或 EXIT
4. X2 月營收年增率突然降溫 → WARNING，非春節才可能升級 EXIT
5. X3 EPS 衰退 → WARNING，若衰退幅度 > 10% 則走 X4

---

## 9. 春節月份特殊規則

春節月份交易日與工作天較少，月營收可能自然降低，因此不可只看單月數字就自動出場。

### 9.1 春節月份啟動條件

以下任一情況啟動春節保護：

- 當月為農曆春節主要落點月份
- 當月或前後月交易日/工作日明顯少於一般月份
- 官方市場開休市表顯示長假連續休市

### 9.2 春節月份處理方式

| 一般月份規則 | 春節月份改法 |
|---|---|
| 單月營收年增率 < 30% 可列出場警訊 | 先標記為 SPRING_FESTIVAL_WATCH，不直接 EXIT |
| 單月營收年增率比上月少超過 20 個百分點 | 先看前後月與合併營收 |
| 單月數字可直接判斷 | 改看 1+2 月合併營收、近 3 個月平均、Q1 財報 |

### 9.3 春節仍應出場的情況

若 Q1 財報出來後確認：

- EPS 年減超過 10%
- 淨利衰退
- 毛利率與營益率明顯轉弱

則不再用春節作為豁免理由，照出場規則處理。

---

## 10. 金融業規則

長輩補充：

> 金融業屬穩定投資行業，利潤被市場主導，一般不考慮，有興趣可以加進來。

預設規則：

- 金控、銀行、保險、證券、期貨等金融業排除在主策略之外
- 使用者可透過設定開關改為納入，但必須標記「金融業資料不完全適用」
- 金融業不應使用存貨週轉率判斷
- 若要納入金融業，需另建金融業專用策略

設定：

```json
{
  "exclude_financial_industry": true,
  "enable_financial_industry_strategy": false
}
```

---

## 11. 資料模型

### 11.1 Company

```ts
type Market = "TWSE" | "TPEX";
type IndustryGroup = "NORMAL" | "FINANCIAL" | "INSURANCE" | "SECURITIES" | "OTHER";

interface Company {
  stockCode: string;
  name: string;
  market: Market;
  industryCode?: string;
  industryName?: string;
  industryGroup: IndustryGroup;
  isFinancial: boolean;
  isInsuranceRelated?: boolean;
  isFirstListed?: boolean;
  isActive: boolean;
}
```

### 11.2 MonthlyRevenue

```ts
interface MonthlyRevenue {
  stockCode: string;
  year: number;
  month: number;
  revenue: number;
  revenueYoY: number;
  cumulativeRevenueYtd: number;
  cumulativeRevenueYoY: number;
  announcedAt?: string;
  source: string;
}
```

### 11.3 QuarterlyFinancial

```ts
interface QuarterlyFinancial {
  stockCode: string;
  fiscalYear: number;
  quarter: 1 | 2 | 3 | 4;
  eps: number;
  epsYoY?: number;
  netIncome: number;
  netIncomeYoY?: number;
  grossMargin?: number;
  operatingMargin?: number;
  inventoryTurnover?: number;
  per?: number;
  announcedAt?: string;
  source: string;
}
```

### 11.4 AnnualFinancial

```ts
interface AnnualFinancial {
  stockCode: string;
  fiscalYear: number;
  netIncome: number;
  eps?: number;
  revenue?: number;
  inventoryTurnover?: number;
  source: string;
}
```

### 11.5 Holding

```ts
interface Holding {
  stockCode: string;
  name?: string;
  shares: number;
  averageCost?: number;
  createdAt: string;
  updatedAt: string;
  notes?: string;
}
```

### 11.6 FundamentalSnapshot

```ts
interface FundamentalSnapshot {
  company: Company;
  latestMonthlyRevenue?: MonthlyRevenue;
  previousMonthlyRevenue?: MonthlyRevenue;
  trailing3MonthRevenueYoYAvg?: number;
  latestQuarterlyFinancial?: QuarterlyFinancial;
  annualFinancials: AnnualFinancial[];
  per?: number;
  inventoryTurnover?: number;
  isSpringFestivalMonth?: boolean;
  dataCompletenessScore: number;
}
```

### 11.7 AnalysisResult

```ts
type Recommendation = "ENTRY" | "HOLD" | "ADD_WATCH" | "WARNING" | "EXIT" | "EXCLUDED" | "INSUFFICIENT_DATA";

type RuleSeverity = "INFO" | "PASS" | "WARNING" | "FAIL";

interface RuleResult {
  ruleId: string;
  label: string;
  passed: boolean;
  severity: RuleSeverity;
  actualValue?: number | string;
  expected?: string;
  message: string;
}

interface AnalysisResult {
  stockCode: string;
  name: string;
  recommendation: Recommendation;
  confidence: number;
  reasons: string[];
  ruleResults: RuleResult[];
  generatedAt: string;
}
```

---

## 12. localStorage 設計

### 12.1 Key

```txt
tw_stock_scanner.holdings.v1
tw_stock_scanner.settings.v1
tw_stock_scanner.onboarding_done.v1
tw_stock_scanner.last_scan_result.v1
```

### 12.2 holdings 結構

```json
[
  {
    "stockCode": "2357",
    "name": "華碩",
    "shares": 1000,
    "averageCost": 520,
    "createdAt": "2026-05-14T00:00:00+08:00",
    "updatedAt": "2026-05-14T00:00:00+08:00"
  }
]
```

### 12.3 輸入解析規則

使用者輸入 `2357 華碩` 時，必須解析為：

```json
{
  "stockCode": "2357",
  "keyword": "華碩"
}
```

不可出現 `undefined`。

必要測試案例：

| 輸入 | 預期 |
|---|---|
| `2357` | stockCode = 2357 |
| `華碩` | match name = 華碩 |
| `2357 華碩` | stockCode = 2357, name = 華碩 |
| ` 2357  華碩 ` | trim 後正確解析 |
| `台積電` | match 2330 台積電 |
| `2330` | match 2330 台積電 |
| 不存在代碼 | 顯示找不到資料，不得新增 undefined |

---

## 13. 前端功能需求

### 13.1 頁面/區塊

1. 首次使用持股輸入 Modal
2. 搜尋列
3. 總覽 Dashboard
4. 進場清單
5. 我的持股
6. 設定
7. 策略規則說明

### 13.2 搜尋列

需求：

- 可輸入繁體中文公司名
- 可輸入股票代碼
- 可輸入代碼 + 公司名
- 即時顯示候選結果
- 可將搜尋結果加入持股
- 可直接查看單檔分析結果

### 13.3 我的持股

功能：

- 新增持股
- 更新股數
- 更新平均成本
- 減碼
- 出清
- 採用出場建議後自動清除或減少持股
- 若持股不存在於資料庫，顯示「找不到資料」，但不得渲染為 undefined

### 13.4 設定開關

至少包含：

| 設定 | 預設 | 說明 |
|---|---|---|
| auto_scan_full_market | true | 是否自動掃描全台上市櫃公司 |
| manual_scan_enabled | true | 是否開啟手動掃描 |
| exclude_financial_industry | true | 是否排除金融業 |
| use_mock_data | true for MVP | 是否使用示範資料 |
| scan_twse | true | 是否掃描上市公司 |
| scan_tpex | true | 是否掃描上櫃公司 |
| spring_festival_guard | true | 是否啟用春節保護 |

### 13.5 RWD

必須支援：

- 桌機寬螢幕
- 筆電
- 平板
- 手機直式
- 手機橫式

最低要求：

- 手機版搜尋框與按鈕不可超出畫面
- 卡片式列表在手機改成單欄
- 表格在手機可水平捲動或改成卡片
- Modal 在手機需滿版或接近滿版

---

## 14. 後端功能需求

### 14.1 建議技術棧

MVP 可使用：

- Python 3.11 或 3.12，避免過新版本相容性問題
- FastAPI
- Uvicorn
- Pydantic
- SQLite
- APScheduler 或系統 cron
- pytest

注意：Windows 開發時啟動建議使用：

```powershell
python -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
```

避免因 PATH 找不到 `uvicorn`。

### 14.2 API 端點

建議端點：

```txt
GET  /api/health
GET  /api/companies/search?q=keyword
GET  /api/companies/{stockCode}
GET  /api/settings
PUT  /api/settings
POST /api/scan/market
POST /api/scan/holdings
GET  /api/scan/runs/latest
POST /api/analyze/{stockCode}
```

使用者持股因存於 localStorage，後端可不保存。若未來要支援登入與跨裝置同步，再新增：

```txt
GET    /api/holdings
POST   /api/holdings
PATCH  /api/holdings/{stockCode}
DELETE /api/holdings/{stockCode}
```

### 14.3 DataProvider 介面

```py
class DataProvider:
    def list_companies(self) -> list[Company]: ...
    def search_companies(self, query: str) -> list[Company]: ...
    def get_monthly_revenue(self, stock_code: str) -> list[MonthlyRevenue]: ...
    def get_quarterly_financials(self, stock_code: str) -> list[QuarterlyFinancial]: ...
    def get_annual_financials(self, stock_code: str) -> list[AnnualFinancial]: ...
    def get_market_holidays(self, year: int) -> list[date]: ...
```

實作類別：

- `MockDataProvider`
- `TwseDataProvider`
- `TpexDataProvider`
- `MopsDataProvider`
- `CompositeDataProvider`

---

## 15. 資料來源規劃

### 15.1 官方資料源

正式版優先使用官方資料源：

- TWSE OpenAPI：上市公司每月營業收入彙總表
- TPEx OpenAPI：上櫃公司每月營業收入彙總表
- 公開資訊觀測站 / 財務相關彙總資料：季報、年報、財務比率
- TWSE 市場開休市日曆

### 15.2 資料轉換原則

外部資料不得直接被規則引擎使用，必須先轉換成：

```txt
Company
MonthlyRevenue
QuarterlyFinancial
AnnualFinancial
FundamentalSnapshot
```

### 15.3 資料品質

每筆分析結果都必須標記：

- 資料來源
- 資料時間
- 缺漏欄位
- dataCompletenessScore

若資料不足，不得硬給 ENTRY 或 EXIT，應顯示 `INSUFFICIENT_DATA`。

---

## 16. 規則引擎

### 16.1 規則引擎輸入

```txt
FundamentalSnapshot + SystemSettings + optional Holding
```

### 16.2 規則引擎輸出

```txt
AnalysisResult
```

### 16.3 進場 pseudo-code

```py
def evaluate_entry(snapshot, settings):
    if settings.exclude_financial_industry and snapshot.company.isFinancial:
        return EXCLUDED

    checks = [
        no_loss_last_5_years(snapshot.annualFinancials),
        net_income_positive_growth_last_3_years(snapshot.annualFinancials),
        cumulative_revenue_yoy(snapshot) >= 50,
        per(snapshot) < 15,
        inventory_turnover(snapshot) > 2.5,
    ]

    if all(checks):
        return ENTRY
    if mostly_pass(checks):
        return WARNING_OR_WATCHLIST
    return INSUFFICIENT_OR_NOT_QUALIFIED
```

### 16.4 持股 pseudo-code

```py
def evaluate_holding(snapshot, holding, settings):
    if settings.exclude_financial_industry and snapshot.company.isFinancial:
        return EXCLUDED

    exit_signals = evaluate_exit_signals(snapshot, settings)

    if has_high_priority_exit(exit_signals):
        return EXIT

    if has_warning(exit_signals):
        return WARNING

    if can_add(snapshot, settings):
        return ADD_WATCH

    return HOLD
```

### 16.5 春節 pseudo-code

```py
def evaluate_exit_signals(snapshot, settings):
    signals = []

    if monthly_revenue_yoy(snapshot) < 30:
        if snapshot.isSpringFestivalMonth and settings.spring_festival_guard:
            signals.append(SPRING_FESTIVAL_WATCH)
        else:
            signals.append(REVENUE_BELOW_30)

    if revenue_yoy_drop_from_previous_month(snapshot) > 20:
        if snapshot.isSpringFestivalMonth and settings.spring_festival_guard:
            signals.append(SPRING_FESTIVAL_WATCH)
        else:
            signals.append(REVENUE_GROWTH_DECELERATION)

    if latest_quarter_eps_yoy(snapshot) <= -10:
        signals.append(EPS_DOWN_OVER_10)

    if latest_quarter_net_income_yoy(snapshot) < 0:
        signals.append(NET_INCOME_DECLINE)

    return signals
```

---

## 17. 掃描服務

### 17.1 掃描模式

| 模式 | 說明 |
|---|---|
| MARKET_SCAN | 掃描全市場，找進場候選 |
| HOLDINGS_SCAN | 掃描使用者持股，判斷續抱/加碼/出場 |
| SINGLE_STOCK_SCAN | 搜尋或點選單檔後分析 |
| SCHEDULED_SCAN | 排程自動掃描 |
| MANUAL_SCAN | 使用者手動掃描 |

### 17.2 排程服務

```py
class ScanScheduler:
    def should_wake_up(today: date, calendar: MarketCalendar) -> WakeUpDecision: ...
    def get_scan_events(today: date) -> list[ScanEvent]: ...
```

WakeUpEvent 類型：

```txt
MONTHLY_REVENUE
Q1_REPORT
Q2_REPORT
Q3_REPORT
ANNUAL_REPORT
SPRING_FESTIVAL_GUARD
MANUAL
```

---

## 18. 報告輸出

### 18.1 無持股報告

欄位：

- 股票代碼
- 公司名稱
- 市場：上市/上櫃
- 推薦狀態：ENTRY / WATCH / EXCLUDED / INSUFFICIENT_DATA
- 今年累計營收年增率
- PER
- 存貨週轉率
- 近 3 年淨利趨勢
- 主要入選原因
- 主要風險

### 18.2 持股報告

欄位：

- 股票代碼
- 公司名稱
- 持有股數
- 平均成本
- 推薦狀態：HOLD / ADD_WATCH / WARNING / EXIT
- 本月營收年增率
- 營收年增率是否降溫
- 最新季 EPS 年增率
- 最新季淨利年增率
- 出場條件是否觸發
- 建議動作
- 可點擊「採用出場建議」

### 18.3 匯出

未來可支援：

- CSV
- Markdown
- PDF
- Email / LINE Notify / Telegram bot

---

## 19. 錯誤處理

### 19.1 前端

- 找不到股票：顯示「找不到資料」，不得產生 undefined card
- 持股股數 <= 0：阻擋輸入
- 平均成本非數字：允許空白，但若有填必須是數字
- localStorage JSON 壞掉：自動備份舊值並重建空陣列

### 19.2 後端

- 外部 API 無回應：回傳資料源錯誤，保留上一份快取
- 部分資料缺漏：結果標為 INSUFFICIENT_DATA
- 排程遇假日：順延
- Windows PATH 問題：README 提醒使用 `python -m uvicorn`

---

## 20. 測試需求

### 20.1 Unit Tests

- 股票搜尋解析
- localStorage schema migration
- 進場規則 E1–E6
- 出場規則 X1–X5
- 春節保護規則
- 金融業排除規則
- 排程 wake up 判斷

### 20.2 Integration Tests

- MockDataProvider → RuleEngine → ScanResult
- Search API
- Market scan API
- Holdings scan API

### 20.3 E2E Tests

- 第一次使用輸入 2357 華碩，不得出現 undefined
- 新增持股後重新整理，持股仍存在
- 按出清後 localStorage 移除該股票
- 手機版新增持股流程正常
- 手動掃描全市場產生進場清單

---

## 21. MVP 範圍

MVP 必須完成：

1. 單頁 RWD 前端
2. 首次使用持股輸入
3. localStorage 持股管理
4. 股票搜尋框
5. MockDataProvider
6. 進場規則
7. 出場規則
8. 春節月份保護
9. 金融業排除
10. 掃描全市場
11. 掃描持股
12. 設定開關
13. Markdown/CSV 報告至少一種

---

## 22. 非 MVP / 後續功能

後續再做：

- 真實官方資料串接
- 自動排程通知
- SQLite / Postgres 儲存掃描歷史
- 使用者登入與跨裝置同步
- 券商 API 串接，自動同步實際持股
- 回測系統
- LINE/Telegram 通知
- 金融業專用策略
- AI 摘要財報附註與重大訊息

---

## 23. 參考資料來源

開發時需以官方來源為準，以下是目前規劃優先參考的來源：

- TWSE OpenAPI: https://openapi.twse.com.tw/
- TPEx OpenAPI: https://www.tpex.org.tw/openapi/
- 證券交易法第 36 條: https://law.fsc.gov.tw/LawContent.aspx?id=FL007009
- 公開發行公司財務報告及營運情形公告申報特殊適用範圍辦法: https://law.fsc.gov.tw/LawContent.aspx?id=GL000593
- TWSE 市場開休市日期: https://www.twse.com.tw/holidaySchedule/holidaySchedule?response=html

