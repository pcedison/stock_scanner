<!-- 感謝貢獻！請填寫以下內容，並勾選已完成的檢查項目。 -->

## 動機 / Motivation

<!-- 這個 PR 解決什麼問題或需求？ -->

## 變更內容 / Changes

<!-- 條列主要變更。 -->

## 測試方式 / How tested

<!-- 你跑了哪些驗證？貼上關鍵結果。 -->

## 檢查清單 / Checklist

- [ ] 本機通過 lint / type gate：`npm run lint`、`python -m ruff check .`、`python -m mypy backend scripts cloudflare tests`
- [ ] 本機通過測試：`python -m pytest -q`（必要時 `npm run test:e2e`）
- [ ] 若改動 API 行為，已同步 FastAPI 與 Cloudflare Worker 的 contract，並更新 `tests/test_api_worker_contracts.py`
- [ ] 若變更 Cloudflare schema，已新增 `cloudflare/migrations/*.sql`（而非只改 `schema.sql`）
- [ ] 一個 PR 只處理一件事，commit 訊息清楚
- [ ] 未在公開處揭露任何安全漏洞（安全問題請循 [SECURITY.md](../SECURITY.md)）
