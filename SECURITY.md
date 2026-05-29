# 安全政策 / Security Policy

感謝你協助讓 Stock Scanner 更安全。本文件說明如何回報漏洞、我們的處理流程，以及支援範圍。

## 支援版本 / Supported Versions

本專案目前以 `main` 分支為唯一支援目標；安全修補只會套用到 `main` 與最新一次部署。

| 版本 | 是否支援安全更新 |
| --- | --- |
| `main`（最新） | ✅ |
| 其他歷史 commit / 分支 | ❌ |

## 回報漏洞 / Reporting a Vulnerability

**請勿在公開的 GitHub Issue、Pull Request 或 Discussion 中揭露漏洞細節。**

請改用以下任一私密管道：

1. **GitHub Private Vulnerability Reporting（建議）**
   前往本 repo 的 **Security → Report a vulnerability** 開啟私密回報。
   （需先在 repo Settings → Security 啟用 *Private vulnerability reporting*。）
2. **Email**：`pcedison@gmail.com`，主旨請加上 `[SECURITY]`。

回報時若能提供以下資訊，將有助於我們更快定位與修復：

- 受影響的元件（FastAPI 後端、Cloudflare Worker、前端、部署流程等）與檔案/路徑。
- 重現步驟或 PoC（proof of concept）。
- 影響範圍評估（資料外洩、權限繞過、RCE、DoS 等）。
- 已知的緩解方式或修補建議（若有）。

## 處理流程與時程 / Response Process

本專案為個人維護的開源專案，將盡力遵循以下時程（以工作日計）：

| 階段 | 目標時程 |
| --- | --- |
| 確認收到回報 | 3 個工作日內 |
| 初步影響評估與分級 | 7 個工作日內 |
| 修補與釋出 | 視嚴重程度，通常 30 天內 |

我們採用**協調式揭露（coordinated disclosure）**：在修補釋出前，請與我們保持私密溝通，並在公開揭露前給予合理的修補時間。修補完成後，我們樂意在 release note 或致謝清單中標註回報者（除非你希望匿名）。

## 範圍 / Scope

**屬於範圍內：**

- 認證與 session 管理（`backend/services/auth.py`、Worker 對應實作）。
- 權限控制與 super user 邊界。
- API 輸入驗證、CSRF/CORS、安全標頭設定。
- 部署流程與機密（secrets）外洩風險。
- 前端 XSS / 注入面向。

**通常不屬於範圍內：**

- 需要實體存取或已被攻陷主機的攻擊情境。
- 缺乏實際可行 PoC 的純理論性問題。
- 第三方相依套件的已知 CVE（這些由 `dependabot`、`pip-audit`、`npm audit` 既有流程追蹤；若你發現我們尚未處理，仍歡迎回報）。
- 社交工程、實體攻擊、以及對非本專案所屬基礎設施的攻擊。

## 既有的安全防護 / Existing Safeguards

本專案在 CI 已內建多層自動化安全檢查，回報前可一併參考：

- `bandit`（Python 靜態安全掃描）
- `pip-audit` / `npm audit`（相依套件漏洞掃描）
- CodeQL（GitHub 程式碼掃描）
- Playwright browser smoke、Worker dry-run、deployment preflight

謝謝你協助保護本專案與其使用者。
