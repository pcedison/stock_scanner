# 台股財報事件驅動掃描器

本專案是台灣上市、上櫃公司基本面掃描工具的 MVP。預設使用官方 TWSE / TPEx 上市櫃公司清單與最新月營收資料，套用「長期賺錢、高成長、估值不貴」與持股出場紀律。輸出僅供研究輔助，不構成投資建議、保證獲利或代客操作。

## 功能狀態

- 前端單頁 RWD 介面
- 首次使用持股輸入與 `localStorage` 持股管理
- 股票搜尋、單檔分析、掃描目前市場、掃描持股
- OfficialDataProvider：TWSE / TPEx 官方公司清單與最新月營收
- MockDataProvider：離線示範公司與示範財報資料
- RuleEngine：進場規則 E1-E6、出場規則 X1-X5
- 設定 API 與前端開關
- 缺少季報、年報、PER、存貨週轉率或毛利率時回傳 `INSUFFICIENT_DATA`

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

這不是逐筆行情 realtime。它是抓取官方已公開的事件型資料。完整基本面策略仍缺官方季報、年報、PER、存貨週轉率與毛利率整合，因此掃描結果會在資料不足時明確標示 `INSUFFICIENT_DATA`，不硬給買賣結論。

若需要離線示範，可在設定中開啟 `使用 Mock Data`，改用 `data/sample_companies.json` 與 `data/sample_fundamentals.json`。
