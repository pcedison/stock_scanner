# Changelog

本專案的所有重要變更都會記錄在此檔。

格式依循 [Keep a Changelog](https://keepachangelog.com/zh-TW/1.1.0/)，
版本號採用 [語意化版本](https://semver.org/lang/zh-TW/)。

## [Unreleased]

### Added
- （尚無）

## [0.1.0] - 2026-05-29

首個標記版本，整合既有的台股掃描能力與專案治理基礎。

### Added
- 台股全市場與持股掃描，含進場、觀察、排除規則。
- 官方資料來源整合：TWSE、TPEx、MOPS 財報與營收資料。
- FastAPI 後端與 Cloudflare Worker 共享 API contract，並以 `tests/test_api_worker_contracts.py` 守護。
- Cloudflare 部署架構：Pages + Python Worker + D1 + R2，seed 可離線重建、驗證、封裝。
- Worker `/api/health` 回報 cache counts 與資料品質狀態。
- 帳號認證與 session 管理（pbkdf2-sha256、登入速率限制、session 上限與清理）。
- 安全標頭（CSP、HSTS、X-Frame-Options 等）、CSRF header 檢查與生產環境 CORS guardrail。
- 開源治理檔案：`LICENSE`（Apache-2.0）、`SECURITY.md`、`CONTRIBUTING.md`、`pyproject.toml`。
- CI 品質閘：`ruff` lint、`mypy`（backend，漸進導入）、`pytest`、`bandit`、`pip-audit`、`npm audit`、CodeQL、Playwright browser smoke、Worker dry-run、wrangler dev smoke、seed quality、deployment preflight。
- R2 seed 上傳對暫時性錯誤加入指數退避重試。

### Changed
- 架構自早期純前端 localStorage MVP 演進為 server-backed：持股、設定、session 以 API 儲存為主，前端 local fallback 僅用於離線降級。
- 將 repo root 的工作文件（spec、tasks、工作報告）移入 `docs/`，root 僅保留標準頂層文件。

[Unreleased]: https://github.com/pcedison/stock_scanner/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/pcedison/stock_scanner/releases/tag/v0.1.0
