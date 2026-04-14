"""Config centralisée — charge le .env et expose les settings."""

import os
from pathlib import Path
from dotenv import load_dotenv

# Charge le .env du projet outreach, puis celui du repo parent en fallback
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")
load_dotenv(_PROJECT_ROOT.parent / ".env")  # fallback repo principal

# ── Supabase ────────────────────────────────────────────────────────────────
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "") or os.getenv("SUPABASE_KEY", "")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")

# ── DeepSeek (optionnel) ────────────────────────────────────────────────────
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

# ── Proxies (optionnel) ────────────────────────────────────────────────────
PROXY_HOST = os.getenv("PROXY_HOST", "")
PROXY_PORT = os.getenv("PROXY_PORT", "12321")
PROXY_USER = os.getenv("PROXY_USER", "")
PROXY_PASS = os.getenv("PROXY_PASS", "")
PROXY_COUNTRY = os.getenv("PROXY_COUNTRY", "fr")

# ── Email IMAP (optionnel) ─────────────────────────────────────────────────
IMAP_HOST = os.getenv("IMAP_HOST", "")
IMAP_USER = os.getenv("IMAP_USER", "")
IMAP_PASS = os.getenv("IMAP_PASS", "")

# ── Resend ─────────────────────────────────────────────────────────────────
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
RESEND_FROM = os.getenv("RESEND_FROM", "devis@vao-solution.com")

# ── Campagne ───────────────────────────────────────────────────────────────
DAILY_SEND_LIMIT = int(os.getenv("DAILY_SEND_LIMIT", "50"))
SEND_DAYS = [d.strip() for d in os.getenv("SEND_DAYS", "monday,wednesday,friday").split(",")]
SEND_HOUR_START = int(os.getenv("SEND_HOUR_START", "8"))
SEND_HOUR_END = int(os.getenv("SEND_HOUR_END", "10"))
SENDER_NAME = os.getenv("SENDER_NAME", "Quentin")
SENDER_PHONE = os.getenv("SENDER_PHONE", "06XXXXXXXX")

# ── Chemins ────────────────────────────────────────────────────────────────
PROJECT_ROOT = _PROJECT_ROOT
TEMPLATES_DIR = _PROJECT_ROOT / "templates"
SCREENSHOTS_DIR = _PROJECT_ROOT / "screenshots"


def proxy_url() -> str | None:
    """Retourne l'URL proxy formatée, ou None si pas configuré."""
    if not PROXY_HOST or not PROXY_USER:
        return None
    return f"http://{PROXY_USER}:{PROXY_PASS}@{PROXY_HOST}:{PROXY_PORT}"


def has_deepseek() -> bool:
    return bool(DEEPSEEK_API_KEY)


def has_proxy() -> bool:
    return bool(PROXY_HOST and PROXY_USER)


def has_imap() -> bool:
    return bool(IMAP_HOST and IMAP_USER and IMAP_PASS)
