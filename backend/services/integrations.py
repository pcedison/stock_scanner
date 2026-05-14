from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class IntegrationStatus:
    name: str
    enabled: bool
    configured: bool
    requires: list[str]
    note: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "configured": self.configured,
            "requires": self.requires,
            "note": self.note,
        }


def _configured(keys: list[str]) -> bool:
    return all(bool(os.getenv(key)) for key in keys)


def integration_status() -> dict[str, Any]:
    telegram_keys = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]
    line_keys = ["LINE_CHANNEL_ACCESS_TOKEN", "LINE_USER_ID"]
    email_keys = ["SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD", "EMAIL_TO"]
    broker_keys = ["BROKER_PROVIDER", "BROKER_API_KEY"]
    ai_keys = ["OPENAI_API_KEY"]

    return {
        "notifications": [
            IntegrationStatus(
                name="telegram",
                enabled=False,
                configured=_configured(telegram_keys),
                requires=telegram_keys,
                note="已保留設定檢查；未提供 bot token 與 chat id 前不會對外發送。",
            ).as_dict(),
            IntegrationStatus(
                name="line",
                enabled=False,
                configured=_configured(line_keys),
                requires=line_keys,
                note="LINE Messaging 需要 channel access token 與收件者 id；未設定時保持停用。",
            ).as_dict(),
            IntegrationStatus(
                name="email",
                enabled=False,
                configured=_configured(email_keys),
                requires=email_keys,
                note="Email 需要 SMTP 與收件者設定；未設定時保持停用。",
            ).as_dict(),
            IntegrationStatus(
                name="desktop",
                enabled=False,
                configured=False,
                requires=[],
                note="瀏覽器桌面通知需要前端權限請求，後端不主動彈通知。",
            ).as_dict(),
        ],
        "broker": IntegrationStatus(
            name="broker",
            enabled=False,
            configured=_configured(broker_keys),
            requires=broker_keys,
            note="券商同步需指定券商與 API 授權；未取得使用者授權前不讀取或同步真實持股。",
        ).as_dict(),
        "aiSummary": IntegrationStatus(
            name="ai_summary",
            enabled=False,
            configured=_configured(ai_keys),
            requires=ai_keys,
            note="AI 財報摘要需要可用模型金鑰與待摘要文件；未設定時僅提供規則摘要。",
        ).as_dict(),
    }
