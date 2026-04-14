"""
Configuration spécifique au déploiement.
Tout ce qui changerait si on déployait le même bot pour un autre client
doit être ici (et nulle part ailleurs).
"""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv("/opt/openclaw/.env")

# ─── RACINE PROJET ───────────────────────────────────────────────────────────
BASE = Path("/opt/openclaw")

# ─── IDENTITÉ BOT ────────────────────────────────────────────────────────────
BOT_NAME = "Azor II"
# Valeur figée côté Supabase Lovable (le dashboard filtre sur cet auteur —
# changer ce littéral casserait la continuité des entrées journal/tâches).
BOT_SUPABASE_AUTHOR = "CroustyLobster"

# ─── FICHIERS MÉMOIRE ────────────────────────────────────────────────────────
JOURNAL_FILE         = BASE / "memory/journal.json"
ALERT_FILE           = BASE / "memory/alert_state.json"
KB_FILE              = BASE / "memory/knowledge_base.json"
LONG_TERM_FILE       = BASE / "memory/long_term.json"
BUSINESS_FILE        = BASE / "memory/business.json"
COSTS_FILE           = BASE / "memory/costs.json"
AUTONOMOUS_LOG_FILE  = BASE / "memory/autonomous_log.json"
AUTONOMOUS_MODE_FILE = BASE / "memory/autonomous_mode.json"
HISTORY_FILE         = BASE / "memory/conv_history.json"

# ─── LIMITES & QUOTAS ────────────────────────────────────────────────────────
EXTRACT_DAILY_LIMIT = 80        # extractions mémoire max par jour
TOKEN_DAILY_LIMIT   = 100_000   # tokens/jour au-delà desquels on coupe extract
MAX_ACTIONS_PER_DAY = 3         # actions autonomes max par jour
HISTORY_LIMIT       = 50        # fenêtre glissante de conv par chat

# ─── CHECK-IN QUOTIDIEN ──────────────────────────────────────────────────────
CHECKIN_DELAY_AFTER_LAST = 30 * 60   # 30 min après la dernière réponse
CHECKIN_HARD_DEADLINE    = 2 * 3600  # 2h max après l'envoi du check-in

# ─── API VAO ─────────────────────────────────────────────────────────────────
API_VAO_URL = os.getenv("API_VAO_URL", "https://178-104-104-36.sslip.io")
API_VAO_KEY = os.getenv("API_VAO_KEY", "")

# ─── SUPABASE (CRM principal + dashboard Lovable) ────────────────────────────
SUPABASE_URL          = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY          = os.getenv("SUPABASE_KEY", "")
SUPABASE_ANON_KEY     = os.getenv("SUPABASE_ANON_KEY", "")
SUPABASE_LOVABLE_URL  = os.getenv("SUPABASE_LOVABLE_URL", "")
SUPABASE_LOVABLE_KEY  = os.getenv("SUPABASE_LOVABLE_SERVICE_KEY", "")

# ─── EMAIL (Resend) ──────────────────────────────────────────────────────────
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
RESEND_FROM    = os.getenv("RESEND_FROM", "alerts@vao-dashboard.xyz")
QUENTIN_EMAIL  = os.getenv("QUENTIN_EMAIL", "")

# ─── PIPELINE EXTERNE (scripts métier VAO) ───────────────────────────────────
PIPELINE_VENV_PYTHON = "/opt/geo-leaad-fr-landscaping/.venv/bin/python"
SCRAPER_SCRIPT       = "/opt/geo-leaad-fr-landscaping/scripts/scheduler.py"
ENRICH_SCRIPT        = "/opt/geo-leaad-fr-landscaping/scripts/enrich.py"
CLEAN_SCRIPT         = "/opt/geo-leaad-fr-landscaping/scripts/clean_leads.py"

# ─── CRM watcher ─────────────────────────────────────────────────────────────
CHAT_ID_LAURIE = os.getenv("TELEGRAM_CHAT_ID_2", "")  # destinataire des notifs CRM

# ─── FONDATEURS (chat_id Telegram → prénom) ──────────────────────────────────
# Source unique de vérité pour adresser un message à un fondateur spécifique
# (check-in du soir, brief matin, entrée journal). Les chat_id sont des chaînes
# pour matcher ce que renvoie l'API Telegram (str(update.effective_chat.id)).
FOUNDERS: dict[str, str] = {
    cid: name
    for cid, name in [
        (os.getenv("TELEGRAM_CHAT_ID_1", ""), "Quentin"),
        (os.getenv("TELEGRAM_CHAT_ID_2", ""), "Laurie"),
    ]
    if cid
}


def founder_name(chat_id: str) -> str:
    """Prénom du fondateur associé à ce chat_id, ou 'Fondateur' si inconnu."""
    return FOUNDERS.get(str(chat_id), "Fondateur")
