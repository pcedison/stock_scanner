# 台股財報事件驅動掃描器

本專案是台灣上市、上櫃公司基本面掃描工具的 MVP。預設使用官方 TWSE / TPEx 上市櫃公司清單與最新月營收資料，套用「長期賺錢、高成長、估值不貴」與持股出場紀律。輸出僅供研究輔助，不構成投資建議、保證獲利或代客操作。

## 功能狀態

- 前端單頁 RWD 介面
- 首次使用持股輸入與 `localStorage` 持股管理
- 股票搜尋、單檔分析、掃描目前市場、掃描持股
- OfficialDataProvider：TWSE / TPEx 官方公司清單、最新月營收、最新季損益與 PER/PBR/殖利率
- 財報 CSV 匯入：可補 EPS YoY、淨利 YoY、毛利率 YoY、歷史年度淨利與存貨週轉率
- 事件驅動自動掃描：關鍵日期依設定自動掃描或提示手動掃描
- Markdown / CSV 掃描報告匯出
- TWSE 開休市日曆更新 API
- CSV 回測骨架：可匯入歷史營收、季報與價格，模擬進出場並計算績效
- 金融業專用策略：不使用存貨週轉率，改看 ROE、逾放比、資本適足率、利差與股利
- 外部整合狀態檢查：通知、券商同步、AI 摘要需金鑰或授權後才啟用
- MockDataProvider：離線示範公司與示範財報資料
- RuleEngine：進場規則 E1-E6、出場規則 X1-X5
- 設定 API 與前端開關
- 缺少季報年增率、年報歷史、存貨週轉率或其他必要欄位時回傳 `INSUFFICIENT_DATA`

## 啟動方式

建議 Python 3.11 或 3.12。

```bash
python -m pip install -r requirements.txt
python -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
```

打開瀏覽器：

```txt
http://127.0.0.1:8000
```

## Windows

PowerShell：

```powershell
.\start_windows.ps1
```

或雙擊：

```txt
start_windows.bat
```

腳本使用 `python -m uvicorn`，避免因 PATH 找不到 `uvicorn`。

## 測試

```bash
pytest
```

## 資料來源

預設資料來源：

- TWSE 上市公司基本資料：`https://openapi.twse.com.tw/v1/opendata/t187ap03_L`
- TPEx 上櫃公司基本資料：`https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O`
- TWSE 上市公司月營收：`https://openapi.twse.com.tw/v1/opendata/t187ap05_L`
- TPEx 上櫃公司月營收：`https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O`
- TWSE 上市公司綜合損益表 / 資產負債表：`https://openapi.twse.com.tw/v1/opendata/t187ap06_L_*`、`https://openapi.twse.com.tw/v1/opendata/t187ap07_L_*`
- TPEx 上櫃公司綜合損益表 / 資產負債表：`https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap06_O_*`、`https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap07_O_*`
- 原 MOPS 歷史合併損益表 / 資產負債表：`https://mopsov.twse.com.tw/mops/web/ajax_t164sb04`、`https://mopsov.twse.com.tw/mops/web/ajax_t164sb03`
- TWSE / TPEx PER、PBR、殖利率資料

這不是逐筆行情 realtime。它是抓取官方已公開的事件型資料。掃描結果會依目前申報窗口判斷「當期已公告」與「當期尚未公告」：例如 5 月中旬會以 Q1 季報是否已出現在官方公開資料中分流；非季報窗口則主要依最新月營收。官方 OpenAPI 可提供最新季損益、資產負債表與估值；系統會把每次取得的最新季資料自動寫入 `data/official_fundamentals_history.json`，並由這份官方歷史快取回推 EPS YoY、淨利 YoY、毛利率變化、Q4 年度淨利與存貨週轉率。快取建立前的歷史缺口可透過原 MOPS 回補端點或 CSV / 授權資料源回補；待補欄位不會硬給買賣結論。

第三方資料平台目前只做合規評估，不做未授權爬取。以財經 M平方為例，公開頁面顯示 API / 數據下載屬訂閱或企業授權服務，且服務條款限制未經書面同意下載、重製或散布數據；若日後取得正式 API 金鑰與授權，再接成獨立 adapter。

## 財報匯入

若要補齊官方歷史快取建立前的舊年度欄位，可複製 `data/fundamentals_import_template.csv` 為 `data/fundamentals_import.csv`，填入真實欄位後重新掃描。此檔案預設不進 git，適合放本機整理過的財報資料。

支援欄位包含：

- 營收輔助：`previous_month_revenue_yoy`、`trailing_3m_average_yoy`、`jan_feb_combined_revenue_yoy`
- 季報：`eps`、`eps_yoy`、`net_income`、`net_income_yoy`、`gross_margin`、`gross_margin_yoy`
- 估值與週轉：`per`、`price_book_ratio`、`dividend_yield`、`inventory_turnover`
- 金融業：`roe`、`non_performing_loan_ratio`、`capital_adequacy_ratio`、`net_interest_margin`
- 年報：`annual_year`、`annual_net_income`

## 報告、排程與回測

- `GET /api/scheduler/auto-scan`：依 wake-up 規則與 `auto_scan_full_market` 決定是否自動掃描。
- `POST /api/data-sources/backfill-history?limit=20`：用原 MOPS 歷史財報批次回補官方歷史快取。
- `POST /api/reports/market?report_format=markdown|csv`：匯出市場掃描報告。
- `POST /api/reports/holdings?report_format=markdown|csv`：匯出持股追蹤報告。
- `POST /api/calendar/{year}/update`：從 TWSE 開休市日期 API 更新本機市場日曆。
- `GET /api/backtest`：讀取 `data/backtest_history.csv` 執行簡易回測；可先複製 `data/backtest_history_template.csv`。

通知、券商同步與 AI 摘要目前提供安全的狀態檢查。未提供 `TELEGRAM_BOT_TOKEN`、`LINE_CHANNEL_ACCESS_TOKEN`、SMTP、券商 API 或 `OPENAI_API_KEY` 前，系統不會對外傳送訊息，也不會讀取真實券商持股。

若需要離線示範，可在設定中開啟 `使用 Mock Data`，改用 `data/sample_companies.json` 與 `data/sample_fundamentals.json`。
