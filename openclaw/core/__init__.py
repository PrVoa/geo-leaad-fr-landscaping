"""Couche infra réutilisable — neutre par rapport au métier."""
from core.shared import (
    bot,
    anthropic_client,
    BOT_TOKEN,
    CHAT_IDS,
    ANTHROPIC_API_KEY,
    now_utc,
    now_paris,
    load_json,
    save_json,
    send,
    broadcast,
    safe_reply,
    authorized,
    strip_markdown,
    escape_markdown,
    TG_LIMIT_HARD,
    TG_SAFE_LIMIT,
)

__all__ = [
    "bot", "anthropic_client",
    "BOT_TOKEN", "CHAT_IDS", "ANTHROPIC_API_KEY",
    "now_utc", "now_paris",
    "load_json", "save_json",
    "send", "broadcast", "safe_reply", "authorized",
    "strip_markdown", "escape_markdown",
    "TG_LIMIT_HARD", "TG_SAFE_LIMIT",
]
