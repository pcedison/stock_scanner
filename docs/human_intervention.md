# 人工介入須知

這份文件記錄目前仍需要人工判讀、授權或憑證的項目。系統會保守處理這些情況：資料未公告或不足時只標示待補，不會硬給進場、續抱或出場結論。

## 1. 官方歷史財報回補狀態

已完成自動化串接的官方來源：

- TWSE / TPEx OpenAPI：上市櫃 universe、最新月營收、最新季損益、資產負債表、PER/PBR/殖利率。
- MOPS 新版官方 API：歷史合併損益表 `t164sb04`、歷史資產負債表 `t164sb03`。
- 本地官方歷史快取：`data/official_fundamentals_history.json`。
- 回補續跑進度：`data/official_history_backfill_progress.json`。

截至 2026-05-14 14:24（Asia/Taipei）已完成的回補結果：

- 官方 universe：1973 檔。
- 已完成歷史回補流程：1883 檔。
- 歷史快取覆蓋：1963 家、16916 筆季度/年度列。
- 當期 pending：579 檔，多數是 2026Q1 尚未在官方列出。
- 真正 failed：90 檔，通常是新掛牌、轉板或指定歷史期別沒有官方列。

`pending` 不代表錯誤。依目前日期 2026-05-14，Q1 一般公司法定公告期限是 2026-05-15 前，金控公司是 2026-05-30 前；這些公司應保留在「尚未公告，無準確資料可供參考」分頁。

## 2. 需要人工判讀的資料例外

以下情況不能用程式硬補：

- 公司掛牌或轉板時間不足 5 年，官方沒有 2021Q4、2023Q4 或 2025Q1 等指定期別。
- 公司曾更名、合併、分割或變更代號，歷史資料需要人工確認是否可接續。
- MOPS 官方 API 回傳無資料列，但公開資訊觀測站頁面或公司年報另有揭露，需要人工交叉確認。
- 金融業專用指標仍未全自動化，例如 ROE、逾放比、資本適足率、淨利差等。

建議處理方式：

- 保留 `failedCompanies` 清單，不把它們歸入已公告可掃描。
- 對歷史不足公司，在 UI 顯示「歷史不足」或「上市年限不足」，不要把 E1/E2 判定為失敗。
- 若人工取得合法授權資料，可用 `data/fundamentals_import.csv` 補齊缺口。

## 3. 重新回補指令

部署用初始快取已打包為：

- `data/official_cache_seed_*.zip`
- matching `data/official_cache_seed_*.sha256` sidecar

部署到私有伺服器時，先把 zip 解壓到專案的 `data/` 目錄，讓伺服器啟動時直接讀取 `official_fundamentals_history.json` 與 `official_history_backfill_progress.json`。之後只跑增量與 pending 重試，不需要每次 deploy 都重新全量回補。

公告期過後可重新跑：

```powershell
python -m backend.services.official_history_backfill --limit 600 --years 5 --mode strategy --throttle 0.03 --reset-progress
```

日常續跑可用：

```powershell
python -m backend.services.official_history_backfill --limit 600 --years 5 --mode strategy --throttle 0.03
```

`strategy` 模式會抓目前策略需要的最小官方期別；若未來要建立完整逐季模型，再改用 `--mode full_quarterly`，但請注意請求量會大幅增加。

## 4. 外部服務仍需人工提供

以下整合需要人工提供憑證或授權：

- LINE 通知：`LINE_CHANNEL_ACCESS_TOKEN`、`LINE_USER_ID`。
- Telegram 通知：`TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`。
- Email 通知：SMTP 主機、帳密與收件人。
- 券商 API：券商、API key、憑證、權限與持股同步規則。
- AI 摘要：模型 API key、摘要範圍與可處理的公開文件來源。

在憑證未設定前，系統應只顯示本機狀態與匯出報告，不自動外送通知或同步券商資料。
