import json, asyncio, datetime, subprocess, shutil, requests, re, time
from pathlib import Path
from collections import deque
import anthropic
import resend
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, MessageHandler, CommandHandler, CallbackQueryHandler,
    filters, ContextTypes,
)

from core.shared import (
    bot,
    anthropic_client,
    BOT_TOKEN,
    CHAT_IDS,
    now_utc,
    now_paris,
    load_json,
    save_json,
    send,
    broadcast,
    safe_reply,
    authorized,
    strip_markdown,
)
from config import (
    BASE,
    JOURNAL_FILE, ALERT_FILE, KB_FILE, BUSINESS_FILE, COSTS_FILE,
    HISTORY_FILE, CHECKIN_STATE_FILE,
    EXTRACT_DAILY_LIMIT, TOKEN_DAILY_LIMIT, HISTORY_LIMIT,
    CHECKIN_CONSOLIDATION_HOUR,
    API_VAO_URL, API_VAO_KEY,
    SUPABASE_URL, SUPABASE_KEY,
    SUPABASE_LOVABLE_URL, SUPABASE_LOVABLE_KEY,
    RESEND_API_KEY, RESEND_FROM, QUENTIN_EMAIL,
    FOUNDERS, founder_name,
    BOT_NAME, BOT_SUPABASE_AUTHOR,
)
from autonomous_loop import (
    _record_stage_started,
    launch_stage,
)
from crm_watcher import start_crm_watcher

# Timeout par défaut pour les appels Anthropic (secondes).
ANTHROPIC_TIMEOUT = 30.0

resend.api_key = RESEND_API_KEY

# Diagnostic au démarrage : présence des credentials Resend
print(
    f"[email] Config Resend — "
    f"RESEND_API_KEY={'OK (' + str(len(RESEND_API_KEY)) + ' chars)' if RESEND_API_KEY else 'MANQUANT'} | "
    f"QUENTIN_EMAIL={QUENTIN_EMAIL or 'MANQUANT'} | "
    f"RESEND_FROM={RESEND_FROM}"
)

client = anthropic_client()

awaiting_journal: set[str] = set()
running_procs: dict[str, subprocess.Popen] = {}

# Debounce : regroupe les messages rapprochés (Telegram découpe les longs messages)
_msg_buffer: dict[str, list[str]] = {}          # chat_id → [textes en attente]
_msg_buffer_update: dict[str, Update] = {}       # chat_id → dernier Update (pour reply)
_msg_buffer_timer: dict[str, asyncio.Task] = {}  # chat_id → timer en cours
_MSG_DEBOUNCE_SEC = 2.0

# État du check-in du jour — journal consolidé après délai (voir scheduler_loop)
# {
#   "date": "YYYY-MM-DD",
#   "sent_at": iso,
#   "responses": {chat_id: {"author": ..., "text": ..., "analyse": ..., "received_at": iso}},
#   "consolidated": False,
# }
checkin_state: dict | None = None

# Fenêtre glissante par chat_id
conv_history: dict[str, deque] = {}


# ─── PERSISTANCE CHECK-IN ────────────────────────────────────────────────────
# checkin_state vit en RAM mais est persisté à chaque mutation. awaiting_journal
# (set des chat_ids en attente d'une réponse) est sérialisé dans le même fichier
# pour qu'un restart entre 21h et 7h ne perde rien.

def _save_checkin_state():
    payload = {
        "checkin_state":   checkin_state,
        "awaiting_journal": sorted(awaiting_journal),
    }
    try:
        save_json(CHECKIN_STATE_FILE, payload)
    except Exception as e:
        print(f"[checkin_state] Erreur sauvegarde : {e}")


def _load_checkin_state():
    """Restaure checkin_state + awaiting_journal au démarrage."""
    global checkin_state
    data = load_json(CHECKIN_STATE_FILE, None)
    if not isinstance(data, dict):
        return
    cs = data.get("checkin_state")
    if isinstance(cs, dict):
        checkin_state = cs
        n = len(cs.get("responses") or {})
        cons = "consolidé" if cs.get("consolidated") else "ouvert"
        print(f"[checkin_state] Restauré : {cs.get('date')} ({cons}, {n} réponse(s))")
    pending = data.get("awaiting_journal") or []
    if isinstance(pending, list):
        for cid in pending:
            awaiting_journal.add(str(cid))
        if pending:
            print(f"[checkin_state] {len(pending)} chat(s) en attente restaurés")

# Alias historique : toutes les ancres `esc(...)` du fichier mappent sur strip_markdown.
esc = strip_markdown

def default_business() -> dict:
    return {
        "leads_total": 0,
        "enrichis": 0,
        "nettoyes": 0,
        "statuts": {},
        "derniere_maj": "",
    }


# ─── MÉMOIRE LONG TERME (Supabase) ───────────────────────────────────────────

def _supabase_crm_headers() -> dict:
    """Headers pour le Supabase CRM principal (table memories)."""
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def add_memory(category: str, content: str, source: str = "conversation",
               founder: str | None = None):
    """INSERT une mémoire dans la table Supabase `memories`."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("[memory] Supabase non configuré — mémoire ignorée")
        return
    now = now_paris()
    iso_cal = now.isocalendar()
    payload = {
        "category": category,
        "content": content,
        "source": source,
        "founder": founder,
        "week_number": iso_cal[1],
        "month_number": now.month,
    }
    try:
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/memories",
            headers=_supabase_crm_headers(),
            json=payload,
            timeout=10,
        )
        if r.status_code in (200, 201):
            print(f"[memory] +1 {category} → Supabase")
        else:
            print(f"[memory] Erreur INSERT {category}: {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"[memory] Erreur réseau INSERT {category}: {e}")


# Cache pour les lectures Supabase (évite de taper la DB à chaque message)
_memories_cache: tuple[float, list[dict]] | None = None
_MEMORIES_CACHE_TTL = 60  # secondes — aligné sur le TTL du system prompt


def load_recent_memories(days: int = 30, limit: int = 30) -> list[dict]:
    """SELECT les mémoires récentes depuis Supabase, avec cache TTL."""
    global _memories_cache
    mono = time.monotonic()
    if _memories_cache and mono - _memories_cache[0] < _MEMORIES_CACHE_TTL:
        return _memories_cache[1]

    if not SUPABASE_URL or not SUPABASE_KEY:
        return []

    cutoff = (now_utc() - datetime.timedelta(days=days)).isoformat()
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/memories",
            headers=_supabase_crm_headers(),
            params={
                "select": "category,content,created_at,founder",
                "created_at": f"gte.{cutoff}",
                "order": "created_at.desc",
                "limit": str(limit),
            },
            timeout=10,
        )
        if r.status_code == 200:
            result = r.json()
            _memories_cache = (mono, result)
            return result
        else:
            print(f"[memory] Erreur SELECT: {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"[memory] Erreur réseau SELECT: {e}")

    # Fallback : retourne le cache périmé si dispo
    if _memories_cache:
        return _memories_cache[1]
    return []


def load_all_memories(category: str | None = None) -> list[dict]:
    """SELECT toutes les mémoires (pour /memoire). Pas de cache."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return []
    params: dict = {
        "select": "category,content,created_at,founder",
        "order": "created_at.desc",
        "limit": "500",
    }
    if category:
        params["category"] = f"eq.{category}"
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/memories",
            headers=_supabase_crm_headers(),
            params=params,
            timeout=10,
        )
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print(f"[memory] Erreur SELECT all: {e}")
    return []


def load_memories_week() -> list[dict]:
    """SELECT les mémoires des 7 derniers jours (hors digests) pour le digest hebdo."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return []
    cutoff = (now_utc() - datetime.timedelta(days=7)).isoformat()
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/memories",
            headers=_supabase_crm_headers(),
            params={
                "select": "category,content,founder,created_at",
                "created_at": f"gte.{cutoff}",
                "source": "not.in.(digest_hebdo,digest_mensuel)",
                "order": "created_at.asc",
                "limit": "200",
            },
            timeout=10,
        )
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print(f"[memory] Erreur SELECT semaine : {e}")
    return []


def load_recent_memories_7d(limit: int = 15) -> list[dict]:
    """Bloc 1 : les 15 mémoires les plus récentes (7 jours), hors digests."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return []
    cutoff = (now_utc() - datetime.timedelta(days=7)).isoformat()
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/memories",
            headers=_supabase_crm_headers(),
            params={
                "select": "category,content,created_at,founder",
                "created_at": f"gte.{cutoff}",
                "source": "not.in.(digest_hebdo,digest_mensuel)",
                "order": "created_at.desc",
                "limit": str(limit),
            },
            timeout=10,
        )
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print(f"[memory] Erreur SELECT 7d : {e}")
    return []


def search_memories_by_keywords(keywords: str, limit: int = 10) -> list[dict]:
    """Bloc 2 : recherche full-text dans les mémoires de +7 jours."""
    if not SUPABASE_URL or not SUPABASE_KEY or not keywords.strip():
        return []
    cutoff = (now_utc() - datetime.timedelta(days=7)).isoformat()
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/memories",
            headers=_supabase_crm_headers(),
            params={
                "select": "category,content,created_at,source",
                "created_at": f"lt.{cutoff}",
                "content": f"fts(french).{keywords}",
                "order": "created_at.desc",
                "limit": str(limit),
            },
            timeout=10,
        )
        if r.status_code == 200:
            return r.json()
        else:
            print(f"[memory] Erreur FTS: {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"[memory] Erreur réseau FTS : {e}")
    return []


def load_recent_digests(limit: int = 3) -> list[dict]:
    """Bloc 3 : les 3 derniers digests (hebdo et/ou mensuel)."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return []
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/memories",
            headers=_supabase_crm_headers(),
            params={
                "select": "content,source,created_at",
                "source": "in.(digest_hebdo,digest_mensuel)",
                "order": "created_at.desc",
                "limit": str(limit),
            },
            timeout=10,
        )
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print(f"[memory] Erreur SELECT digests : {e}")
    return []


def load_digests_for_month(month: int, year: int) -> list[dict]:
    """SELECT les digests hebdo d'un mois donné (pour le digest mensuel)."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return []
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/memories",
            headers=_supabase_crm_headers(),
            params={
                "select": "content,week_number,created_at",
                "source": "eq.digest_hebdo",
                "month_number": f"eq.{month}",
                "order": "week_number.asc",
                "limit": "10",
            },
            timeout=10,
        )
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print(f"[memory] Erreur SELECT digests mois : {e}")
    return []


def extract_keywords(text: str, max_words: int = 5) -> str:
    """Extrait les mots-clés significatifs d'un message utilisateur.
    Retourne une chaîne de mots séparés par des espaces (pour plainto_tsquery),
    ou chaîne vide si rien de significatif.
    """
    words = re.findall(r'\b\w+\b', text.lower())
    # Garder uniquement les mots de 4+ caractères, hors stop words
    significant = [
        w for w in words
        if len(w) >= 4 and w not in _STOP_WORDS
    ]
    # Dédupliquer en gardant l'ordre
    seen: set[str] = set()
    unique: list[str] = []
    for w in significant:
        if w not in seen:
            seen.add(w)
            unique.append(w)
        if len(unique) >= max_words:
            break
    return " ".join(unique)


# ─── PRINCIPES FONDATEURS ────────────────────────────────────────────────────

_PRINCIPLES_FILE = BASE / "memory/principles.json"
_principles_cache: list[str] | None = None


def load_principles() -> list[str]:
    """Charge les principes fondateurs depuis le fichier JSON."""
    global _principles_cache
    if _principles_cache is not None:
        return _principles_cache
    if not _PRINCIPLES_FILE.exists():
        return []
    try:
        with open(_PRINCIPLES_FILE, encoding="utf-8") as f:
            data = json.load(f)
        _principles_cache = data if isinstance(data, list) else []
        return _principles_cache
    except Exception:
        return []


def save_principles(principles: list[str]):
    """Sauvegarde les principes fondateurs et invalide le cache."""
    global _principles_cache
    with open(_PRINCIPLES_FILE, "w", encoding="utf-8") as f:
        json.dump(principles, f, ensure_ascii=False, indent=2)
    _principles_cache = principles


# ─── DIGESTS ─────────────────────────────────────────────────────────────────

async def generate_weekly_digest(convergence_text: str = ""):
    """Génère un digest hebdomadaire à partir des mémoires de la semaine.

    Si `convergence_text` est fourni (et non vide / non "Aucune convergence"),
    on l'injecte dans le prompt pour que le digest l'intègre."""
    memories = load_memories_week()
    if not memories:
        print("[digest] Aucune mémoire cette semaine — pas de digest.")
        return

    entries_text = "\n".join(
        f"- [{m.get('category','?')}] {m.get('content','')}"
        for m in memories
    )
    convergence_block = ""
    if convergence_text and "Aucune convergence" not in convergence_text:
        convergence_block = (
            "\n\nConvergence détectée entre les fondateurs cette semaine :\n"
            f"{convergence_text}\n"
            "Intègre cette convergence dans le digest si pertinent."
        )
    prompt = (
        "Voici toutes les décisions, apprentissages et erreurs de la semaine "
        "pour le projet VAO (SaaS devis paysagistes).\n\n"
        f"{entries_text}\n\n"
        "Produis un digest de 10-15 lignes maximum qui résume : les décisions clés "
        "prises, ce qu'on a appris, les erreurs à ne pas reproduire. "
        "Sois factuel et concis. Format texte brut, pas de bullet points."
        f"{convergence_block}"
    )
    try:
        resp = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=600,
            timeout=ANTHROPIC_TIMEOUT,
            messages=[{"role": "user", "content": prompt}],
        )
        digest = resp.content[0].text.strip()
    except Exception as e:
        print(f"[digest] Erreur Claude : {e}")
        return

    paris = now_paris()
    week_num = paris.isocalendar()[1]
    add_memory(
        category="digest",
        content=digest,
        source="digest_hebdo",
        founder=None,
    )
    print(f"[memory] Digest semaine {week_num} → Supabase")


async def generate_monthly_digest():
    """Génère un digest mensuel à partir des digests hebdo du mois précédent."""
    paris = now_paris()
    # Mois précédent
    if paris.month == 1:
        prev_month, prev_year = 12, paris.year - 1
    else:
        prev_month, prev_year = paris.month - 1, paris.year

    weekly_digests = load_digests_for_month(prev_month, prev_year)
    if not weekly_digests:
        print(f"[digest] Aucun digest hebdo pour {prev_year}-{prev_month:02d} — pas de digest mensuel.")
        return

    digests_text = "\n\n---\n\n".join(
        f"Semaine {d.get('week_number', '?')} :\n{d.get('content', '')}"
        for d in weekly_digests
    )
    prompt = (
        f"Voici les digests hebdomadaires du mois {prev_month:02d}/{prev_year} "
        "pour le projet VAO.\n\n"
        f"{digests_text}\n\n"
        "Consolide en un résumé mensuel de 5-10 lignes : les grandes orientations "
        "du mois, les tournants, ce qui a changé. Sois factuel et concis."
    )
    try:
        resp = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=400,
            timeout=ANTHROPIC_TIMEOUT,
            messages=[{"role": "user", "content": prompt}],
        )
        digest = resp.content[0].text.strip()
    except Exception as e:
        print(f"[digest] Erreur Claude mensuel : {e}")
        return

    add_memory(
        category="digest",
        content=digest,
        source="digest_mensuel",
        founder=None,
    )
    print(f"[memory] Digest mensuel {prev_year}-{prev_month:02d} → Supabase")



# ─── DAILY UPDATES (Supabase) ────────────────────────────────────────────────
# Capture au fil de l'eau ce que chaque fondateur partage au bot dans la journée.
# Sert de source pour le brief croisé du lendemain matin.

def insert_daily_update(founder: str, content: str, source: str = "conversation"):
    """INSERT une update dans daily_updates."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("[daily_update] Supabase non configuré — update ignorée")
        return
    payload = {
        "founder": founder,
        "content": content,
        "source": source,
        "date": now_paris().date().isoformat(),
    }
    try:
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/daily_updates",
            headers=_supabase_crm_headers(),
            json=payload,
            timeout=10,
        )
        if r.status_code in (200, 201):
            print(f"[daily_update] +1 {source} ({founder}) → Supabase")
        else:
            print(f"[daily_update] Erreur INSERT: {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"[daily_update] Erreur réseau INSERT: {e}")


def get_daily_updates(founder: str, date_iso: str) -> list[dict]:
    """SELECT les updates d'un fondateur pour une date donnée."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return []
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/daily_updates",
            headers=_supabase_crm_headers(),
            params={
                "select": "content,source,created_at",
                "founder": f"eq.{founder}",
                "date": f"eq.{date_iso}",
                "order": "created_at.asc",
            },
            timeout=10,
        )
        if r.status_code == 200:
            return r.json()
        print(f"[daily_update] Erreur SELECT: {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"[daily_update] Erreur réseau SELECT: {e}")
    return []


def has_daily_updates_today(founder: str) -> bool:
    """True si ce fondateur a déjà des updates aujourd'hui."""
    today = now_paris().date().isoformat()
    updates = get_daily_updates(founder, today)
    return len(updates) > 0


def get_daily_updates_range(founder: str, since_iso: str) -> list[dict]:
    """SELECT les updates d'un fondateur depuis une date (incluse), tri ASC."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return []
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/daily_updates",
            headers=_supabase_crm_headers(),
            params={
                "select": "content,source,date,created_at",
                "founder": f"eq.{founder}",
                "date": f"gte.{since_iso}",
                "order": "created_at.asc",
            },
            timeout=10,
        )
        if r.status_code == 200:
            return r.json()
        print(f"[daily_update] Erreur SELECT range: {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"[daily_update] Erreur réseau SELECT range: {e}")
    return []


def cleanup_old_daily_updates(days: int = 7):
    """DELETE les daily_updates de plus de N jours."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return
    cutoff = (now_paris().date() - datetime.timedelta(days=days)).isoformat()
    try:
        r = requests.delete(
            f"{SUPABASE_URL}/rest/v1/daily_updates",
            headers=_supabase_crm_headers(),
            params={"date": f"lt.{cutoff}"},
            timeout=10,
        )
        if r.status_code in (200, 204):
            print(f"[daily_update] Nettoyage : entrées avant {cutoff} supprimées")
        else:
            print(f"[daily_update] Erreur DELETE: {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"[daily_update] Erreur réseau DELETE: {e}")


async def extract_daily_update(user_text: str, founder: str):
    """
    Appel Haiku pour déterminer si le message contient une info sur ce que le
    fondateur a fait/avancé/décidé aujourd'hui. Si oui, INSERT dans daily_updates.
    Partage le cap quotidien d'extractions avec extract_and_save_memory.
    """
    # Garde-fou : message trop court
    if len(user_text.split()) < 30:
        return

    # Garde-fou : cap quotidien partagé
    if not can_extract():
        return

    prompt = (
        "Ce message d'un fondateur de startup contient-il une information sur ce qu'il "
        "a fait, avancé, décidé, appris ou rencontré comme difficulté aujourd'hui ?\n\n"
        "Si OUI, extrais un résumé factuel en 1-2 phrases (pas de \"il a dit que\", "
        "juste les faits). Si NON, réponds exactement : NON\n\n"
        f"Message :\n\"\"\"\n{user_text}\n\"\"\""
    )
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            timeout=ANTHROPIC_TIMEOUT,
            messages=[{"role": "user", "content": prompt}],
        )
        usage = resp.usage
        track_tokens(usage.input_tokens, usage.output_tokens)
        increment_extraction()

        raw = resp.content[0].text.strip()
        if raw.upper().startswith("NON"):
            return
        # Haiku a extrait un résumé → INSERT
        insert_daily_update(founder, raw, source="conversation")
    except Exception as e:
        print(f"[daily_update] Erreur extraction : {type(e).__name__}: {e}")


# ─── MÉMOIRE BUSINESS ─────────────────────────────────────────────────────────

def refresh_business():
    """Met à jour business.json depuis l'API VAO."""
    stats = get_vao_stats()
    if "error" in stats:
        return
    business = load_json(BUSINESS_FILE, default_business())
    business.update({
        "leads_total": stats.get("total_leads", stats.get("leads", business.get("leads_total", 0))),
        "enrichis":    stats.get("enrichis", business.get("enrichis", 0)),
        "nettoyes":    stats.get("nettoyes", business.get("nettoyes", 0)),
        "statuts":     stats.get("repartition_statut", business.get("statuts", {})),
        "derniere_maj": now_utc().isoformat(),
    })
    save_json(BUSINESS_FILE, business)

def load_business() -> dict:
    return load_json(BUSINESS_FILE, default_business())


# ─── COMPTEUR DE COÛTS ────────────────────────────────────────────────────────

def default_costs() -> dict:
    return {"tokens_today": 0, "extractions_today": 0, "tokens_month": 0, "last_reset": ""}

def load_costs() -> dict:
    return load_json(COSTS_FILE, default_costs())

def track_tokens(input_tok: int, output_tok: int):
    """Ajoute les tokens consommés et remet à zéro si nouveau jour."""
    costs = load_costs()
    today = now_utc().date().isoformat()
    if costs.get("last_reset", "")[:10] != today:
        costs["tokens_today"]     = 0
        costs["extractions_today"] = 0
        costs["last_reset"]       = now_utc().isoformat()
    total = input_tok + output_tok
    costs["tokens_today"]  += total
    costs["tokens_month"]  += total
    if costs["tokens_today"] > TOKEN_DAILY_LIMIT:
        print(f"[costs] WARNING — {costs['tokens_today']} tokens aujourd'hui, extraction désactivée.")
    save_json(COSTS_FILE, costs)

def can_extract() -> bool:
    """True si on n'a pas atteint les limites journalières."""
    costs = load_costs()
    today = now_utc().date().isoformat()
    if costs.get("last_reset", "")[:10] != today:
        return True  # nouveau jour, compteurs pas encore remis à zéro
    return (
        costs.get("tokens_today", 0) < TOKEN_DAILY_LIMIT
        and costs.get("extractions_today", 0) < EXTRACT_DAILY_LIMIT
    )

def increment_extraction():
    costs = load_costs()
    today = now_utc().date().isoformat()
    if costs.get("last_reset", "")[:10] != today:
        costs["tokens_today"]     = 0
        costs["extractions_today"] = 0
        costs["last_reset"]       = now_utc().isoformat()
    costs["extractions_today"] = costs.get("extractions_today", 0) + 1
    save_json(COSTS_FILE, costs)


# ─── JOURNAL ──────────────────────────────────────────────────────────────────

def append_journal(author_id: str, author_name: str, content: str, analyse: str = ""):
    entries = load_json(JOURNAL_FILE, [])
    entries.append({
        "date":        now_utc().isoformat(),
        "auteur":      author_name,
        "author_id":   author_id,
        "contenu":     content,
        "analyse":     analyse,
    })
    save_json(JOURNAL_FILE, entries)


# ─── VAO API ──────────────────────────────────────────────────────────────────

_vao_stats_cache: tuple[float, dict] | None = None
_VAO_STATS_TTL = 300  # 5 min — l'API se rafraîchit à l'heure, mais on garde du grain pour /stats

def get_vao_stats() -> dict:
    global _vao_stats_cache
    now = time.monotonic()
    if _vao_stats_cache and now - _vao_stats_cache[0] < _VAO_STATS_TTL:
        return _vao_stats_cache[1]
    try:
        r = requests.get(
            f"{API_VAO_URL}/stats",
            headers={"X-API-Key": API_VAO_KEY},
            timeout=10,
        )
        result = r.json() if r.status_code == 200 else {"error": f"HTTP {r.status_code}"}
    except Exception as e:
        result = {"error": str(e)}
    # On ne cache pas les erreurs (sinon une indispo bloque tout pendant 5 min)
    if "error" not in result:
        _vao_stats_cache = (now, result)
    return result

def get_disk_pct() -> float:
    total, used, _ = shutil.disk_usage("/")
    return round(used / total * 100, 1)

def get_ram_free_pct() -> float | None:
    try:
        info = {}
        for line in open("/proc/meminfo"):
            parts = line.split()
            info[parts[0].rstrip(":")] = int(parts[1])
        return round(info["MemAvailable"] / info["MemTotal"] * 100, 1)
    except Exception:
        return None


# ─── RAG ──────────────────────────────────────────────────────────────────────

# Mots vides FR+EN à ignorer dans la recherche (évite les faux positifs)
_STOP_WORDS = {
    'tu', 'as', 'les', 'le', 'la', 'un', 'une', 'des', 'de', 'et', 'en', 'est',
    'il', 'elle', 'ils', 'elles', 'nous', 'vous', 'ce', 'ca', 'se', 'si',
    'ne', 'pas', 'plus', 'sur', 'par', 'au', 'aux', 'du', 'me', 'te', 'je', 'on',
    'que', 'qui', 'ou', 'mais', 'avec', 'pour', 'dans', 'ai', 'son', 'sa', 'mon',
    'ma', 'ton', 'ta', 'nos', 'vos', 'leur', 'leurs', 'cet', 'cette', 'ces',
    'the', 'is', 'in', 'of', 'to', 'and', 'for', 'this', 'that', 'it', 'are',
    'by', 'at', 'be', 'an',
}

_kb_cache: tuple[float, list] | None = None
_KB_CACHE_TTL = 120  # secondes — la KB ne change qu'à /ingest


def _invalidate_kb_cache():
    global _kb_cache
    _kb_cache = None


def load_kb() -> list | None:
    """Charge knowledge_base.json avec cache TTL 120s.
    Le cache est invalidé manuellement après une ingestion (_ingest_file)."""
    global _kb_cache
    mono = time.monotonic()
    if _kb_cache and mono - _kb_cache[0] < _KB_CACHE_TTL:
        return _kb_cache[1]
    if not KB_FILE.exists():
        return None
    try:
        with open(KB_FILE, encoding="utf-8") as f:
            kb = json.load(f)
        if isinstance(kb, list):
            _kb_cache = (mono, kb)
            return kb
        return None
    except Exception as e:
        print(f"[kb] Erreur chargement : {e}")
        return None

def search_kb(query: str, top_k: int = 3) -> list[str]:
    kb = load_kb()
    if not kb:
        return []
    # Mots 2+ chars, stop words filtrés pour éviter les faux positifs
    words = set(re.findall(r'\b\w{2,}\b', query.lower())) - _STOP_WORDS
    if not words:
        return []
    scores: dict[int, int] = {}
    for i, chunk in enumerate(kb):
        # Cherche dans content + source + id pour trouver les transcripts P1/P2...
        haystack = " ".join([
            chunk.get("content", ""),
            chunk.get("source", ""),
            chunk.get("id", ""),
        ]).lower()
        chunk_words = set(re.findall(r'\b\w{2,}\b', haystack)) - _STOP_WORDS
        score = len(words & chunk_words)
        if score > 0:
            scores[i] = score
    top_indices = sorted(scores, key=lambda x: scores[x], reverse=True)[:top_k]
    print(f"[KB] search_kb('{query[:50]}') → {len(scores)} matches, top {len(top_indices)} retenus (mots: {words})")
    return [kb[i]["content"] for i in top_indices]


# ─── SYSTEM PROMPT ENRICHI ────────────────────────────────────────────────────

_sys_prompt_base_cache: tuple[float, str, set[str]] | None = None
_SYS_PROMPT_BASE_TTL = 60  # secondes — recharge principes/KB/blocs 1+3 au max 1×/min

_CAT_LABELS = {
    "decision": "Décisions prises",
    "apprentissage": "Apprentissages",
    "erreur": "Erreurs passées à éviter",
    "fait": "Faits notables",
    "preference": "Préférences fondateurs",
    "digest": "Digest",
}


def _format_memories_by_cat(memories: list[dict]) -> list[str]:
    """Groupe des mémoires par catégorie et retourne des lignes formatées."""
    by_cat: dict[str, list[dict]] = {}
    for m in memories:
        cat = m.get("category", "autre")
        by_cat.setdefault(cat, []).append(m)
    lines: list[str] = []
    for cat, items in by_cat.items():
        label = _CAT_LABELS.get(cat, cat.capitalize())
        lines.append(f"{label} :")
        for m in items:
            date = m.get("created_at", "")[:10]
            lines.append(f"  [{date}] {m.get('content', '')}")
    return lines


def _memory_dedup_key(item: dict) -> str:
    """Clé de dédup : 50 premiers caractères du contenu, normalisés."""
    return (item.get("content") or "")[:50].strip().lower()


def build_system_prompt(user_query: str | None = None) -> str:
    """Construit le system prompt enrichi.
    - Principes fondateurs (toujours présents)
    - Bloc 3 : digests récents
    - Bloc 1 : mémoires des 7 derniers jours
    - Bloc 2 : mémoires liées au message (recherche full-text, si user_query)
    - KB (sources listées seulement, plus d'injection automatique de chunks —
      le RAG dans _process_message s'en charge avec la query du user)
    Le cache TTL s'applique aux blocs 1+3+principes+KB (la partie statique) ET
    aux clés de dédup du bloc 1, pour que le bloc 2 puisse filtrer les doublons.
    Le bloc 2 est recalculé à chaque appel avec user_query.
    """
    global _sys_prompt_base_cache
    mono = time.monotonic()

    # Partie statique (cachée 60s) — on cache aussi les clés du bloc 1 pour dédup
    if _sys_prompt_base_cache and mono - _sys_prompt_base_cache[0] < _SYS_PROMPT_BASE_TTL:
        base_prompt, bloc1_keys = _sys_prompt_base_cache[1], _sys_prompt_base_cache[2]
    else:
        base = (BASE / "system_prompt.txt").read_text(encoding="utf-8")
        parts = [base]

        # Principes fondateurs
        principles = load_principles()
        if principles:
            parts.append("\n\n--- PRINCIPES FONDATEURS — ne jamais contredire ---")
            for p in principles:
                parts.append(f"- {p}")

        # Bloc 3 — Digests récents
        digests = load_recent_digests(limit=3)
        if digests:
            parts.append("\n\n--- DIGEST RÉCENT ---")
            for d in digests:
                src = d.get("source", "digest")
                label = "Mensuel" if src == "digest_mensuel" else "Hebdo"
                date = d.get("created_at", "")[:10]
                parts.append(f"[{label} — {date}]\n{d.get('content', '')}")

        # Bloc 1 — Mémoire récente (7 jours)
        recent = load_recent_memories_7d(limit=15)
        bloc1_keys = {_memory_dedup_key(m) for m in recent if m.get("content")}
        parts.append("\n\n--- MÉMOIRE RÉCENTE (7 jours) ---")
        if recent:
            parts.extend(_format_memories_by_cat(recent))
        else:
            parts.append("Aucune mémoire cette semaine.")

        # KB — uniquement le catalogue des sources, plus d'injection auto
        kb = load_kb()
        if kb:
            sources = sorted({c.get("source", "") for c in kb if c.get("source")})
            transcript_sources = [s for s in sources if "transcript" in s.lower() or re.match(r'P\d+', s)]
            other_sources = [s for s in sources if s not in transcript_sources]
            parts.append("\n--- BASE DE CONNAISSANCE (KB) ---")
            parts.append(f"Tu as accès à {len(kb)} chunks dans ta KB.")
            parts.append(f"TRANSCRIPTIONS DISPONIBLES ({len(transcript_sources)}) : {', '.join(transcript_sources)}")
            parts.append(f"Autres sources ({len(other_sources)}) : {', '.join(other_sources[:30])}")

        base_prompt = "\n".join(parts)
        _sys_prompt_base_cache = (mono, base_prompt, bloc1_keys)

    # Bloc 2 — Mémoire liée à la conversation (pas caché, dépend du message).
    # Filtre les items déjà présents dans le bloc 1 (même contenu sur 50 chars).
    if user_query:
        kw = extract_keywords(user_query, max_words=5)
        if kw:
            relevant = search_memories_by_keywords(kw, limit=10)
            relevant = [m for m in relevant if _memory_dedup_key(m) not in bloc1_keys]
            if relevant:
                extra_lines = ["\n\n--- MÉMOIRE LIÉE À CETTE CONVERSATION ---"]
                extra_lines.extend(_format_memories_by_cat(relevant))
                return base_prompt + "\n".join(extra_lines)

    return base_prompt


# ─── HISTORIQUE DE CONVERSATION ───────────────────────────────────────────────

def get_history(chat_id: str) -> list[dict]:
    if chat_id not in conv_history:
        conv_history[chat_id] = deque(maxlen=HISTORY_LIMIT)
    return list(conv_history[chat_id])


_history_save_pending = False   # coalesce : on écrit au max 1×/5s pendant une rafale


def _save_history_to_disk():
    """Sérialise conv_history dans HISTORY_FILE. Appelé en arrière-plan."""
    data = {cid: list(dq) for cid, dq in conv_history.items()}
    try:
        save_json(HISTORY_FILE, data)
    except Exception as e:
        print(f"[history] Erreur sauvegarde : {e}")


async def _debounced_save_history():
    global _history_save_pending
    if _history_save_pending:
        return
    _history_save_pending = True
    try:
        await asyncio.sleep(5)
        await asyncio.to_thread(_save_history_to_disk)
    finally:
        _history_save_pending = False


def add_to_history(chat_id: str, role: str, content: str):
    if chat_id not in conv_history:
        conv_history[chat_id] = deque(maxlen=HISTORY_LIMIT)
    conv_history[chat_id].append({"role": role, "content": content})
    # Persistance disque, coalescée pour éviter d'écrire à chaque message.
    try:
        asyncio.get_event_loop().create_task(_debounced_save_history())
    except RuntimeError:
        # Pas de loop active (ex: import time) — on ignore, sera sauvé au prochain ajout.
        pass


def load_history_from_disk():
    """Restaure conv_history depuis HISTORY_FILE au démarrage."""
    try:
        data = load_json(HISTORY_FILE, {})
    except Exception as e:
        print(f"[history] Erreur chargement : {e}")
        return
    restored = 0
    for cid, msgs in (data or {}).items():
        dq = deque(maxlen=HISTORY_LIMIT)
        for m in msgs[-HISTORY_LIMIT:]:
            if isinstance(m, dict) and "role" in m and "content" in m:
                dq.append(m)
        conv_history[cid] = dq
        restored += len(dq)
    if restored:
        print(f"[history] {restored} messages restaurés pour {len(conv_history)} chat(s)")


# ─── EXTRACTION MÉMOIRE ───────────────────────────────────────────────────────

_extract_cap_last_log = 0.0   # throttle du log "cap atteint" (1 fois / heure)


async def extract_and_save_memory(user_text: str, ai_response: str,
                                  founder: str | None = None):
    """
    Appel Haiku léger pour extraire décisions/apprentissages/erreurs.
    Garde-fous : message > 50 mots, cap journalier (EXTRACT_DAILY_LIMIT),
    cap tokens (TOKEN_DAILY_LIMIT).
    Écrit dans Supabase `memories` (plus de cap de 100).
    """
    global _extract_cap_last_log

    # Garde-fou 1 : message trop court → pas d'extraction
    if len(user_text.split()) < 50:
        return

    # Garde-fou 2 : limites journalières atteintes
    if not can_extract():
        # Log throttlé : 1 ligne / heure max pour signaler qu'on sature.
        now = time.monotonic()
        if now - _extract_cap_last_log > 3600:
            costs = load_costs()
            print(
                f"[memory_extract] Cap atteint — "
                f"{costs.get('extractions_today', 0)}/{EXTRACT_DAILY_LIMIT} extractions, "
                f"{costs.get('tokens_today', 0):,}/{TOKEN_DAILY_LIMIT:,} tokens. "
                f"Extractions skip jusqu'à minuit UTC."
            )
            _extract_cap_last_log = now
        return

    prompt = (
        "Analyse cet échange entre un fondateur et son assistant IA.\n"
        "Si et seulement si l'échange contient une décision explicite, un apprentissage "
        "ou une erreur identifiée, retourne un JSON avec ce format :\n"
        '{"decisions": ["..."], "apprentissages": ["..."], "erreurs": ["..."]}\n'
        "Si rien de significatif, retourne exactement : {}\n"
        "Sois très sélectif — ne mémorise que ce qui est concret et actionnable.\n\n"
        f"Fondateur: {user_text}\n\n"
        f"Assistant: {ai_response}"
    )
    # Mapping catégories Haiku → catégories Supabase (singulier)
    _cat_map = {"decisions": "decision", "apprentissages": "apprentissage", "erreurs": "erreur"}
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            timeout=ANTHROPIC_TIMEOUT,
            messages=[{"role": "user", "content": prompt}],
        )
        # Comptabilise les tokens
        usage = resp.usage
        track_tokens(usage.input_tokens, usage.output_tokens)
        increment_extraction()

        raw = resp.content[0].text.strip()
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if not match:
            return
        extracted = json.loads(match.group())
        for haiku_cat, supabase_cat in _cat_map.items():
            for item in extracted.get(haiku_cat, []):
                if item and len(item) > 10:
                    add_memory(supabase_cat, item, source="conversation",
                               founder=founder)
    except Exception as e:
        # Ne jamais bloquer la conversation, mais laisser une trace pour debug.
        print(f"[memory_extract] Erreur : {type(e).__name__}: {e}")


# ─── ANALYSE CHECK-IN ─────────────────────────────────────────────────────────

async def analyze_checkin(author_name: str, content: str) -> str:
    """Claude analyse l'entrée de journal et retourne un commentaire court."""
    memories = load_recent_memories(days=14, limit=10)
    decision_items = [
        f"- {m.get('content','')}"
        for m in memories
        if m.get("category") == "decision"
    ][:5]
    decisions_recent = "\n".join(decision_items) or "Aucune."

    prompt = (
        f"{author_name} a dit aujourd'hui sur VAO :\n\"{content}\"\n\n"
        f"Décisions récentes en mémoire :\n{decisions_recent}\n\n"
        "En 2-3 phrases max : commente ce qu'il a fait, "
        "note si un engagement a été pris, et challenge si nécessaire. "
        "Tutoie, sois direct, en français."
    )
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            timeout=ANTHROPIC_TIMEOUT,
            system=build_system_prompt(),
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip()
    except Exception as e:
        return f"(Analyse impossible : {e})"


# ─── DIVERS ───────────────────────────────────────────────────────────────────

def tail_logs(n: int = 20) -> str:
    log = Path("/opt/geo-leaad-fr-landscaping/logs/app.log")
    if not log.exists():
        return "Fichier de log introuvable."
    try:
        r = subprocess.run(["tail", "-n", str(n), str(log)], capture_output=True, text=True)
        return r.stdout or "Log vide."
    except Exception as e:
        return f"Erreur : {e}"


# ─── SUPABASE LOVABLE — TÂCHES ───────────────────────────────────────────────

def _supabase_lovable_headers() -> dict:
    return {
        "apikey": SUPABASE_LOVABLE_KEY,
        "Authorization": f"Bearer {SUPABASE_LOVABLE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


async def insert_journal_supabase(
    titre: str,
    contenu: str,
    tags: list[str] | None = None,
    auteur: str = BOT_SUPABASE_AUTHOR,
) -> dict:
    """
    Insère une entrée dans la table `journal` du Supabase Lovable (dashboard VAO).
    Anti-doublon : si une entrée avec le même titre existe déjà aujourd'hui,
    on ne ré-insère pas (retourne {ok: True, skipped: True, id: existing_id}).

    Colonnes écrites : titre, contenu, auteur, semaine, tags.
    `created_at` est rempli automatiquement par Postgres.
    """
    if not SUPABASE_LOVABLE_URL or not SUPABASE_LOVABLE_KEY:
        return {"ok": False, "error": "Supabase Lovable non configuré"}
    try:
        import httpx
    except ImportError:
        return {"ok": False, "error": "httpx non installé"}

    today    = datetime.date.today()
    iso      = today.isocalendar()
    # Format attendu par le dashboard Lovable : "YYYY-Sww" (ex "2026-S15")
    semaine  = f"{iso.year}-S{iso.week:02d}"
    today_iso = today.isoformat()
    headers  = _supabase_lovable_headers()

    async with httpx.AsyncClient(timeout=15.0) as http:
        # 1. Anti-doublon : titre identique sur la journée courante
        try:
            check = await http.get(
                f"{SUPABASE_LOVABLE_URL}/rest/v1/journal",
                headers=headers,
                params={
                    "titre":      f"eq.{titre}",
                    "created_at": f"gte.{today_iso}",
                    "select":     "id",
                    "limit":      "1",
                },
            )
            if check.status_code == 200 and check.json():
                existing_id = check.json()[0].get("id")
                print(f"[journal] Doublon détecté ({titre!r}) — id existant: {existing_id}")
                return {"ok": True, "id": existing_id, "skipped": True}
        except Exception as e:
            # Non-bloquant : on tente l'insert quand même
            print(f"[journal] Anti-doublon GET échoué : {e}")

        # 2. Insertion
        payload = {
            "titre":   titre,
            "contenu": contenu,
            "auteur":  auteur,
            "semaine": semaine,
            "tags":    tags or [],
        }
        print(f"[journal] Tentative insertion : titre={titre!r} semaine={semaine} tags={tags or []}")
        try:
            r = await http.post(
                f"{SUPABASE_LOVABLE_URL}/rest/v1/journal",
                headers=headers,
                json=payload,
            )
        except Exception as e:
            print(f"[journal] Erreur réseau : {e}")
            return {"ok": False, "error": str(e)}

        if r.status_code in (200, 201):
            rows = r.json() if r.content else []
            new_id = rows[0].get("id") if rows else None
            print(f"[journal] Succès — id={new_id}")
            return {"ok": True, "id": new_id, "skipped": False}

        err = f"Supabase {r.status_code}: {r.text[:300]}"
        print(f"[journal] Échec : {err}")
        return {"ok": False, "error": err}


async def insert_tasks_supabase(tasks: list[dict]) -> dict:
    """Insère des tâches dans Supabase Lovable. Retourne {inserted, skipped, error}."""
    if not SUPABASE_LOVABLE_URL or not SUPABASE_LOVABLE_KEY:
        return {"inserted": [], "skipped": [], "error": "Supabase Lovable non configuré"}
    try:
        import httpx
    except ImportError:
        return {"inserted": [], "skipped": [], "error": "httpx non installé"}

    tomorrow = (datetime.datetime.utcnow() + datetime.timedelta(days=1)).date().isoformat()
    inserted, skipped = [], []

    async with httpx.AsyncClient(timeout=15.0) as http:
        for t in tasks:
            payload = {
                "title": t["title"],
                "status": "todo",
                "assigned_to": t.get("assigned_to", "Quentin"),
                "tag": t.get("tag", "Admin"),
                "created_by": BOT_SUPABASE_AUTHOR,
                "due_date": t.get("due_date") or tomorrow,
            }
            try:
                r = await http.post(
                    f"{SUPABASE_LOVABLE_URL}/rest/v1/tasks",
                    headers=_supabase_lovable_headers(),
                    json=payload,
                )
                if r.status_code in (200, 201):
                    inserted.append(t)
                else:
                    return {"inserted": inserted, "skipped": skipped,
                            "error": f"Supabase {r.status_code}: {r.text[:200]}"}
            except Exception as e:
                return {"inserted": inserted, "skipped": skipped, "error": str(e)}

    return {"inserted": inserted, "skipped": skipped, "error": None}


# ─── DÉTECTION TÂCHES (Haiku, multi, autonome) ───────────────────────────────

VALID_TAGS = ["Scraping", "Prospection", "Dev", "Admin"]

# Trigger explicite : seule une demande explicite "ajoute une tâche / nouvelle
# tâche / rajoute / crée une tâche / mets dans le trello" déclenche l'insertion
# en flow normal. Le check-in du soir et /push restent les autres canaux.
# Trigger explicite : déclencheurs (verbe) + cible (où ranger).
# On exige les deux pour éviter les faux positifs sur "ajoute" employé hors-tâche.
TASK_TRIGGER_VERB_RE = re.compile(
    r'\b('
    r'ajoute|rajoute|cr[ée]+e?|cr[ée]er|mets?|met(?:tre|s)?|'
    r'pousse(?:r|z)?|envoie(?:r|z)?|push(?:e|er)?|'
    r'enregistre(?:r|z)?|note(?:r|z)?|inscri(?:s|re|t|vez)|'
    r'nouvelle|nouvelles'
    r')\b',
    re.IGNORECASE,
)
TASK_TRIGGER_TARGET_RE = re.compile(
    r'\b('
    r't[âa]che(?:s)?|action(?:s)?\s+(?:concr[èe]te(?:s)?|à\s+faire)|'
    r'trello|dashboard|kanban|tableau|board|supabase|lovable|'
    r'to-?do|todo(?:list)?|liste\s+(?:des\s+)?(?:t[âa]ches|actions|todo)'
    r')\b',
    re.IGNORECASE,
)
def is_explicit_task_request(text: str) -> bool:
    return bool(TASK_TRIGGER_VERB_RE.search(text) and TASK_TRIGGER_TARGET_RE.search(text))

# Détection d'une question explicite : "?" ou mot interrogatif clair.
# Sert à décider si, après ajout d'une tâche, il faut quand même répondre.
QUESTION_RE = re.compile(
    r'(\?'
    r'|\b(pourquoi|comment|quand|combien|qui|quoi|où|ou est|est-?ce que|'
    r'explique(?:s|z|-moi)?|dis-?moi|peux-?tu|peut-?on|sais-?tu|'
    r'qu[\'’]est-?ce|quelle?s?)\b)',
    re.IGNORECASE,
)

# Mots-clés qui justifient l'injection des stats VAO dans le user message.
# Hors de ce périmètre, on respecte la consigne du system prompt : pas de stats
# CRM dans la conversation.
STATS_TRIGGER_RE = re.compile(
    r'\b(stat(?:s|istique)?s?|leads?|pipeline|scraper|enrich(?:issement)?|'
    r'chiffres?|donn[ée]es?|combien)\b',
    re.IGNORECASE,
)


async def extract_tasks_list(text: str, is_conversation: bool = False) -> list[dict]:
    """Haiku extrait UNE OU PLUSIEURS tâches d'un texte (message, réponse ou conversation)."""
    today = datetime.date.today().isoformat()
    tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
    if is_conversation:
        intro = (
            f"Tu reçois un extrait de conversation Telegram entre Quentin (le fondateur) "
            f"et son assistant {BOT_NAME}. Extrait TOUTES les actions concrètes "
            "discutées ou suggérées que Quentin n'a pas explicitement refusées : "
            "engagements pris, idées validées, suggestions de l'assistant que "
            "Quentin a reprises ou n'a pas écartées. Sois généreux : si une action "
            "est mentionnée comme \"à faire\", \"il faut\", \"je vais\", ou si "
            "l'assistant propose et Quentin enchaîne sans dire non, c'est une tâche.\n"
        )
        label = "Conversation"
    else:
        intro = "Extrait toutes les tâches/actions concrètes à faire de ce texte.\n"
        label = "Texte"
    prompt = (
        f"{intro}"
        f"Date du jour : {today}\n\n"
        f"{label} :\n\"\"\"\n{text}\n\"\"\"\n\n"
        "Réponds UNIQUEMENT en JSON valide sans markdown, sous la forme :\n"
        '{"tasks": [{"title": "...", "assigned_to": "Quentin ou Laurie", '
        '"tag": "Scraping ou Prospection ou Dev ou Admin", '
        '"due_date": "YYYY-MM-DD"}]}\n\n'
        "Règles :\n"
        "- title : reformule en titre court actionnable (impératif)\n"
        "- une entrée par action distincte\n"
        "- assigned_to : Quentin par défaut sauf si Laurie est mentionnée\n"
        "- tag : déduis du contexte, Admin par défaut\n"
        f"- due_date : {tomorrow} par défaut sauf si une date est mentionnée\n"
        "- Si vraiment aucune action concrète : retourne {\"tasks\": []}"
    )
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            timeout=ANTHROPIC_TIMEOUT,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        raw = re.sub(r'^```(?:json)?\s*', '', raw)
        raw = re.sub(r'\s*```$', '', raw)
        data = json.loads(raw)
        cleaned = []
        for t in data.get("tasks", []):
            if not isinstance(t, dict) or not t.get("title"):
                continue
            if t.get("assigned_to") not in ("Quentin", "Laurie"):
                t["assigned_to"] = "Quentin"
            if t.get("tag") not in VALID_TAGS:
                t["tag"] = "Admin"
            cleaned.append(t)
        return cleaned
    except Exception as e:
        print(f"[task_extract_multi] Erreur : {e}")
        return []


def format_inserted_tasks(inserted: list[dict], header: str) -> str:
    lines = [header]
    for t in inserted:
        lines.append(
            f"• {t['title']} → {t.get('assigned_to', 'Quentin')} ({t.get('tag', 'Admin')})"
        )
    return "\n".join(lines)


# ─── TÂCHES PLANIFIÉES ────────────────────────────────────────────────────────

async def _generate_brief_text(recipient: str = "Quentin") -> str:
    """Génère le texte du brief matin (pur, sans envoi) adressé à `recipient`."""
    paris     = now_paris()
    today_str = paris.strftime("%d/%m/%Y")

    # Agents actifs (données brutes)
    agents_data = []
    for name, p in running_procs.items():
        agents_data.append(f"{name}: {'actif' if p.poll() is None else 'arrêté'}")
    agents_text = ", ".join(agents_data) if agents_data else "aucun agent actif"

    # Stats VAO + refresh business
    stats = get_vao_stats()
    refresh_business()

    if "error" not in stats:
        leads     = stats.get("total_leads", stats.get("leads", "?"))
        nouveaux  = stats.get("repartition_statut", {}).get("nouveau", "?")
        contactes = stats.get("repartition_statut", {}).get("contacte", "?")
        offres    = stats.get("repartition_statut", {}).get("offre_envoyee", "?")
        stats_text = f"leads={leads}, nouveaux={nouveaux}, contactés={contactes}, offres={offres}"
    else:
        stats_text = f"API indisponible ({stats['error']})"

    # Rappels Supabase
    rappels_data: list[str] = []
    if SUPABASE_URL and SUPABASE_KEY:
        try:
            today_iso = paris.date().isoformat()
            r = requests.get(
                f"{SUPABASE_URL}/rest/v1/campaign_leads",
                headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
                params={"rappel_le": f"eq.{today_iso}", "select": "nom,entreprise,telephone"},
                timeout=10,
            )
            if r.status_code == 200:
                for row in r.json()[:10]:
                    nom = row.get("nom") or "?"
                    ent = row.get("entreprise") or ""
                    tel = row.get("telephone") or ""
                    label = nom + (f" ({ent})" if ent else "") + (f" — {tel}" if tel else "")
                    rappels_data.append(label)
        except Exception as e:
            print(f"[brief] Erreur Supabase rappels : {e}")
    rappels_text = "; ".join(rappels_data) if rappels_data else "aucun rappel"

    # Journal de la semaine
    journal      = load_json(JOURNAL_FILE, [])
    cutoff       = now_utc() - datetime.timedelta(days=7)
    recent       = [e for e in journal if datetime.datetime.fromisoformat(e["date"]) > cutoff]
    journal_text = "\n".join(
        f"- [{e['date'][:10]}] {e.get('auteur', e.get('author_name','?'))}: {e.get('contenu', e.get('content',''))}"
        for e in recent
    ) or "aucune entrée cette semaine"

    # Brief généré par Claude — format libre, adressé nommément au destinataire
    prompt = (
        f"On est le {today_str}. Génère le brief matin pour {recipient} sur VAO "
        f"(tutoie-le/la, commence par le/la saluer par son prénom).\n\n"
        f"Données disponibles :\n"
        f"- Agents : {agents_text}\n"
        f"- Stats VAO : {stats_text}\n"
        f"- Rappels du jour : {rappels_text}\n"
        f"- Journal récent :\n{journal_text}\n\n"
        "Salue-le brièvement, donne l'essentiel, pointe l'objectif prioritaire de la journée. "
        "Sois direct, naturel, sans titres ni listes à puces forcées."
    )
    try:
        resp = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=400,
            timeout=ANTHROPIC_TIMEOUT,
            system=build_system_prompt(),
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip()
    except Exception as e:
        return f"Brief du {today_str} — agents: {agents_text}. Stats: {stats_text}. (Claude indispo: {e})"


async def morning_brief():
    """Brief croisé du matin : chaque fondateur reçoit un résumé conversationnel
    de TOUT ce que l'AUTRE a partagé hier — conversations libres, check-in,
    messages à 15h ou 23h. Source : table daily_updates.
    Silence complet si l'autre n'a rien partagé.
    """
    yesterday = (now_paris().date() - datetime.timedelta(days=1)).isoformat()

    for cid in CHAT_IDS:
        name = founder_name(cid)
        others = [(oid, oname) for oid, oname in FOUNDERS.items() if str(oid) != str(cid)]
        for oid, oname in others:
            updates = get_daily_updates(oname, yesterday)
            if not updates:
                continue  # l'autre n'a rien partagé → silence

            # Fusionner toutes les entrées de la journée
            all_content = "\n\n".join(
                f"[{u.get('source', 'conversation')}] {u.get('content', '')}"
                for u in updates
            )
            if not all_content.strip():
                continue

            prompt = (
                f"Tu résumes à {name} ce que {oname} a partagé hier sur VAO. "
                f"Voici TOUT ce que {oname} a partagé au fil de la journée "
                f"(conversations, check-in, messages divers). "
                f"Ton conversationnel, comme un associé qui raconte autour d'un café, en français. "
                f"Salue {name} par son prénom, tutoie-le/la. "
                f"Simplifie le jargon technique si besoin (Laurie n'est pas dev, Quentin n'est pas commercial). "
                f"Ne parle pas de toi : pas de \"j'ai fait\", pas de stats CRM, pas de rapport d'infra, "
                f"pas de \"demain je vais...\". Juste ce que {oname} a partagé. "
                f"Si les infos se recoupent, fusionne. Pas de bullet points. "
                f"3 à 6 lignes.\n\n"
                f"Ce que {oname} a partagé hier :\n\"\"\"\n{all_content}\n\"\"\""
            )
            try:
                resp = client.messages.create(
                    model="claude-sonnet-4-20250514",
                    max_tokens=400,
                    timeout=ANTHROPIC_TIMEOUT,
                    system=build_system_prompt(),
                    messages=[{"role": "user", "content": prompt}],
                )
                msg = resp.content[0].text.strip()
            except Exception as e:
                print(f"[brief] Erreur Claude brief croisé pour {name} : {e}")
                msg = f"Bonjour {name}, hier {oname} a partagé : {all_content[:500]}"
            await send(cid, msg)


async def daily_checkin():
    """Envoie la question check-in personnalisée à chaque fondateur.
    Si le fondateur a déjà partagé des infos dans la journée (daily_updates),
    la question est adaptée : rappel pour compléter plutôt que question ouverte.
    """
    global checkin_state
    today_iso = now_paris().date().isoformat()
    checkin_state = {
        "date":         today_iso,
        "sent_at":      now_utc().isoformat(),
        "responses":    {},
        "consolidated": False,
    }
    _save_checkin_state()
    for cid in CHAT_IDS:
        name = founder_name(cid)
        already_shared = has_daily_updates_today(name)

        if already_shared:
            # Le fondateur a déjà partagé des infos aujourd'hui
            checkin_prompt = (
                f"C'est le check-in du soir avec {name}. "
                f"{name} t'a déjà partagé des choses aujourd'hui dans vos conversations. "
                f"Écris-lui UN message court pour lui demander s'il y a autre chose à ajouter "
                f"avant que tu fasses le résumé pour l'autre fondateur demain matin. "
                f"Tutoie-le/la, naturel, 1-2 phrases."
            )
            fallback = (
                f"Bonsoir {name}, j'ai noté ce que tu m'as partagé aujourd'hui. "
                f"Est-ce qu'il y a autre chose à ajouter avant que je fasse le résumé demain matin ?"
            )
        else:
            checkin_prompt = (
                f"C'est l'heure du check-in du soir avec {name}, fondateur de VAO. "
                f"Écris-lui UN message court adressé directement à {name} "
                f"(commence par le saluer par son prénom) pour lui demander ce qu'il/elle "
                f"a fait sur VAO aujourd'hui. Une ou deux phrases, naturel, tutoie-le/la."
            )
            fallback = f"Bonsoir {name}, qu'est-ce que t'as avancé sur VAO aujourd'hui ?"

        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=80,
                timeout=ANTHROPIC_TIMEOUT,
                system=build_system_prompt(),
                messages=[{"role": "user", "content": checkin_prompt}],
            )
            question = resp.content[0].text.strip()
        except Exception as e:
            print(f"[checkin] Erreur génération question pour {name} : {e}")
            question = fallback

        awaiting_journal.add(cid)
        _save_checkin_state()
        await send(cid, question)


async def expand_founder_update(author: str, raw_text: str) -> str:
    """
    Demande à Sonnet de produire un compte-rendu détaillé et fidèle de ce qu'a
    partagé un fondateur lors du check-in. Le but : capturer toute la richesse
    sans rien inventer — actions menées, décisions, intentions, blocages,
    contexte, prochains pas. Format prose, pas de bullets forcés.
    """
    if not raw_text or not raw_text.strip():
        return "(aucune réponse)"
    prompt = (
        f"{author} vient de partager ses avancées du jour sur VAO lors du check-in du soir. "
        f"Voici son message brut :\n\n\"\"\"\n{raw_text}\n\"\"\"\n\n"
        "Rédige un compte-rendu détaillé et fidèle de ce qu'il a partagé pour le journal "
        "de bord du dashboard. Capture tout ce qui est dit : actions menées, décisions "
        "prises, intentions, blocages rencontrés, prochains pas, contexte. Sois exhaustif "
        "et structuré, sans rien inventer ni broder. Si plusieurs sujets sont abordés, "
        "structure-les. Prose naturelle, pas de bullets forcés. Reste en troisième personne "
        "(\"Quentin a fait…\", \"Laurie a décidé…\")."
    )
    try:
        resp = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1500,
            timeout=ANTHROPIC_TIMEOUT,
            system=build_system_prompt(),
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip()
    except Exception as e:
        print(f"[expand_founder] Erreur Claude : {e}")
        # Fallback : on retourne au moins le texte brut pour ne rien perdre
        return raw_text


def get_code_updates_today() -> str:
    """
    Retourne un résumé court (1-2 lignes) des commits du jour sur le repo
    geo-leaad-fr-landscaping, ou chaîne vide si rien.
    """
    repo = "/opt/geo-leaad-fr-landscaping"
    try:
        r = subprocess.run(
            ["git", "-C", repo, "log",
             "--since=midnight", "--pretty=format:%s"],
            capture_output=True, text=True, timeout=5,
        )
        commits = [l.strip() for l in r.stdout.splitlines() if l.strip()]
    except Exception as e:
        print(f"[code_updates] Erreur git log : {e}")
        return ""
    if not commits:
        return ""
    n = len(commits)
    # Une seule ligne factuelle, sans détail technique
    if n == 1:
        return f"Côté code aujourd'hui : 1 commit ({commits[0][:120]})."
    return f"Côté code aujourd'hui : {n} commits — dernier : {commits[0][:120]}."


async def consolidate_checkin():
    """
    Écrit les réponses du check-in dans le journal local ET dans la table
    journal du dashboard Supabase Lovable, puis reset l'état.
    """
    global checkin_state
    if not checkin_state or checkin_state.get("consolidated"):
        return
    checkin_state["consolidated"] = True
    _save_checkin_state()
    responses = checkin_state.get("responses", {})

    if not responses:
        # Aucune réponse — silencieux, pas d'entrée journal
        print(f"[checkin] Aucune réponse au check-in du {checkin_state['date']}, journal vide.")
        return

    # 1. Journal local (memory/journal.json) — une entrée par auteur
    for cid, r in responses.items():
        append_journal(cid, r["author"], r["text"], r.get("analyse", ""))

    # 2. Dashboard Supabase Lovable — une entrée consolidée pour la journée
    # Format daily : avancées des fondateurs DÉTAILLÉES + mention code minimaliste
    date_iso = checkin_state["date"]
    auteurs  = sorted({r["author"] for r in responses.values()})
    contenu_parts = []
    for r in responses.values():
        # Demande à Claude un compte-rendu détaillé et fidèle de ce qu'a dit le fondateur
        detailed = await expand_founder_update(r["author"], r["text"])
        bloc = f"## {r['author']}\n\n{detailed}"
        contenu_parts.append(bloc)
    contenu = "\n\n".join(contenu_parts)

    # Mention code en fin d'entrée si pertinent (1-2 lignes max, pas de détail)
    code_line = get_code_updates_today()
    if code_line:
        contenu += f"\n\n---\n{code_line}"

    titre = f"Check-in du {date_iso}"
    tags  = ["check-in"] + auteurs

    try:
        result = await insert_journal_supabase(
            titre=titre,
            contenu=contenu,
            tags=tags,
            auteur=BOT_SUPABASE_AUTHOR,
        )
        if result.get("ok"):
            if result.get("skipped"):
                print(f"[checkin] Dashboard : entrée déjà présente (id={result.get('id')})")
            else:
                print(f"[checkin] Dashboard : entrée ajoutée (id={result.get('id')})")
        else:
            print(f"[checkin] Dashboard : échec insertion — {result.get('error')}")
    except Exception as e:
        print(f"[checkin] Dashboard : exception {type(e).__name__}: {e}")

    n = len(responses)
    # Pas de broadcast : le résultat servira au brief croisé du lendemain matin.
    print(f"[checkin] Journal consolidé pour {checkin_state['date']} ({n} réponse(s)).")


def get_code_updates_week() -> str:
    """Liste les commits de la semaine écoulée sur le repo, format compact."""
    repo = "/opt/geo-leaad-fr-landscaping"
    try:
        r = subprocess.run(
            ["git", "-C", repo, "log",
             "--since=7 days ago", "--pretty=format:%ad %s", "--date=short"],
            capture_output=True, text=True, timeout=5,
        )
        commits = [l.strip() for l in r.stdout.splitlines() if l.strip()]
    except Exception as e:
        print(f"[code_updates] Erreur git log hebdo : {e}")
        return "Aucun commit récupéré."
    if not commits:
        return "Aucun commit cette semaine."
    return "\n".join(f"- {c}" for c in commits[:30])


async def weekly_summary():
    """
    Récap hebdomadaire dominical — détaillé. Pousse une entrée longue dans le
    journal du dashboard et broadcast un résumé sur Telegram.
    """
    journal  = load_json(JOURNAL_FILE, [])
    cutoff   = now_utc() - datetime.timedelta(days=7)
    week     = [e for e in journal if datetime.datetime.fromisoformat(e["date"]) > cutoff]
    memories = load_recent_memories(days=7, limit=30)
    business = load_business()

    if not week:
        await broadcast("Aucune entrée de journal cette semaine — pas de récap à faire.")
        return

    journal_text = "\n".join(
        f"- [{e['date'][:10]}] {e.get('auteur', e.get('author_name','?'))}: {e.get('contenu', e.get('content',''))}"
        for e in week
    )
    decisions_text = "\n".join(
        f"- {m.get('content','')}" for m in memories if m.get("category") == "decision"
    ) or "Aucune."
    apprent_text = "\n".join(
        f"- {m.get('content','')}" for m in memories if m.get("category") == "apprentissage"
    ) or "Aucun."
    erreurs_text = "\n".join(
        f"- {m.get('content','')}" for m in memories if m.get("category") == "erreur"
    ) or "Aucune."
    code_text = get_code_updates_week()

    prompt = (
        f"Récap hebdomadaire du projet VAO. Aujourd'hui c'est dimanche, on rentre dans le détail.\n\n"
        f"Journal de la semaine (avancées des fondateurs jour par jour) :\n{journal_text}\n\n"
        f"Commits de la semaine sur le repo :\n{code_text}\n\n"
        f"Décisions récentes en mémoire :\n{decisions_text}\n\n"
        f"Apprentissages récents :\n{apprent_text}\n\n"
        f"Erreurs/frictions identifiées :\n{erreurs_text}\n\n"
        f"Stats actuelles : leads={business.get('leads_total')}, "
        f"statuts={json.dumps(business.get('statuts',{}))}\n\n"
        "Rédige un récap hebdo détaillé pour le dashboard. Raconte ce qui s'est "
        "passé jour par jour côté humain (Quentin, Laurie), puis évoque les changements "
        "code de manière structurée, puis les décisions prises et les apprentissages. "
        "Format prose naturelle, pas de bullets forcés sauf quand pertinent. "
        "Long format autorisé (récap hebdo détaillé)."
    )
    try:
        resp    = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2000,
            timeout=ANTHROPIC_TIMEOUT,
            system=build_system_prompt(),
            messages=[{"role": "user", "content": prompt}],
        )
        summary = resp.content[0].text.strip()
    except Exception as e:
        summary = f"Erreur génération récap hebdo : {e}"

    # Broadcast Telegram
    await broadcast(summary)

    # Push dans le journal du dashboard avec tag hebdo
    paris = now_paris()
    iso = paris.date().isocalendar()
    titre = f"Récap hebdo {iso.year}-S{iso.week:02d}"
    try:
        r = await insert_journal_supabase(
            titre=titre,
            contenu=summary,
            tags=["hebdo", "recap"],
            auteur=BOT_SUPABASE_AUTHOR,
        )
        if r.get("ok"):
            print(f"[weekly] Récap hebdo poussé dashboard (id={r.get('id')}, skipped={r.get('skipped')})")
        else:
            print(f"[weekly] Échec push dashboard : {r.get('error')}")
    except Exception as e:
        print(f"[weekly] Exception push dashboard : {e}")


def send_email_resend(to: str, subject: str, body: str) -> dict:
    """
    Envoie un email via Resend avec logs explicites.
    Retourne {ok: bool, id|error: str}.
    """
    print(f"[email] Tentative envoi à {to} (sujet: {subject!r}, from: {RESEND_FROM})...")
    if not RESEND_API_KEY:
        print("[email] Erreur : RESEND_API_KEY manquant dans .env")
        return {"ok": False, "error": "RESEND_API_KEY manquant"}
    if not to:
        print("[email] Erreur : destinataire vide")
        return {"ok": False, "error": "destinataire vide"}
    try:
        resp = resend.Emails.send({
            "from":    RESEND_FROM,
            "to":      [to],
            "subject": subject,
            "text":    body,
        })
        # resend SDK retourne dict avec "id" en cas de succès
        email_id = resp.get("id") if isinstance(resp, dict) else None
        if email_id:
            print(f"[email] Succès — id={email_id}")
            return {"ok": True, "id": email_id}
        # Pas d'id mais pas d'exception : on log la réponse brute
        print(f"[email] Réponse Resend ambiguë : {resp!r}")
        return {"ok": True, "id": str(resp)}
    except Exception as e:
        # Resend lève des exceptions avec détails (domain not verified, etc.)
        print(f"[email] Erreur : {type(e).__name__}: {e}")
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


async def send_weekly_prof_email():
    """Génère et envoie le rapport hebdo profs via Resend — vendredi 18h."""
    if not RESEND_API_KEY or not QUENTIN_EMAIL:
        print("[email] RESEND_API_KEY ou QUENTIN_EMAIL manquant, rapport hebdo ignoré.")
        return

    journal   = load_json(JOURNAL_FILE, [])
    cutoff    = now_utc() - datetime.timedelta(days=7)
    week      = [e for e in journal if datetime.datetime.fromisoformat(e["date"]) > cutoff]
    business  = load_business()
    memories  = load_recent_memories(days=7, limit=10)

    journal_text = "\n".join(
        f"- [{e['date'][:10]}] {e.get('auteur','?')}: {e.get('contenu','')}"
        for e in week
    ) or "Aucune entrée cette semaine."
    decisions_text = "\n".join(
        f"- {m.get('content','')}"
        for m in memories if m.get("category") == "decision"
    ) or "Aucune."

    prompt = (
        f"Tu es {BOT_NAME}, co-fondateur virtuel de VAO.\n"
        f"Génère un rapport hebdomadaire à destination des professeurs ESCP (500 mots max).\n"
        f"Format EXACT :\n"
        f"## Avancement\n[ce qui a été fait]\n\n"
        f"## Compétences mobilisées\n[compétences ESCP appliquées]\n\n"
        f"## Prochaines étapes\n[plan semaine prochaine]\n\n"
        f"Journal de la semaine :\n{journal_text}\n\n"
        f"Décisions récentes :\n{decisions_text}\n\n"
        f"Stats : leads={business.get('leads_total')}, statuts={json.dumps(business.get('statuts', {}))}\n\n"
        f"Ton : professionnel mais concret. En français."
    )
    try:
        resp = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=800,
            timeout=ANTHROPIC_TIMEOUT,
            messages=[{"role": "user", "content": prompt}],
        )
        body = resp.content[0].text.strip()
    except Exception as e:
        body = f"Erreur génération rapport : {e}"

    paris     = now_paris()
    week_str  = paris.strftime("semaine du %d/%m/%Y")
    subject   = f"Rapport hebdomadaire VAO — {week_str}"

    result = send_email_resend(QUENTIN_EMAIL, subject, body)
    if result["ok"]:
        await broadcast(f"📧 Rapport hebdo envoyé à {QUENTIN_EMAIL}")
    else:
        await broadcast(f"❌ Erreur envoi rapport hebdo : {result['error']}")


# Texte de convergence détecté le dimanche 19h, consommé par le digest hebdo à 20h.
_pending_convergence_text: str = ""


async def detect_convergence() -> str:
    """Repère 1 à 3 sujets que les deux fondateurs ont mentionnés indépendamment
    cette semaine, ou des connexions entre leurs activités. Retourne le texte
    Sonnet ou '' si pas assez de données / erreur."""
    week_start = (now_paris().date() - datetime.timedelta(days=7)).isoformat()
    by_founder: dict[str, str] = {}
    for founder in sorted(set(FOUNDERS.values())):
        updates = get_daily_updates_range(founder, week_start)
        if updates:
            content = "\n".join(f"- {u.get('content', '')}" for u in updates if u.get("content"))
            if content.strip():
                by_founder[founder] = content

    if len(by_founder) < 2:
        print(f"[convergence] Moins de 2 fondateurs avec des updates ({list(by_founder)}), skip.")
        return ""

    sections = "\n\n".join(f"=== {name} ===\n{content}" for name, content in by_founder.items())
    prompt = (
        f"Voici ce que chaque fondateur a partagé cette semaine sur VAO :\n\n"
        f"{sections}\n\n"
        "Identifie 1 à 3 sujets que les deux ont mentionnés indépendamment, "
        "ou des connexions entre leurs activités qu'ils n'ont peut-être pas vues. "
        "Si aucune convergence notable, réponds exactement : "
        "Aucune convergence détectée cette semaine.\n"
        "Sois concis."
    )
    try:
        resp = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=400,
            timeout=ANTHROPIC_TIMEOUT,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip()
    except Exception as e:
        print(f"[convergence] Erreur Claude : {type(e).__name__}: {e}")
        return ""


async def scheduler_loop():
    last_checkin_date     = None
    last_summary_week     = None
    last_digest_week      = None
    last_brief_date       = None
    last_prof_email_week  = None
    last_monthly_digest   = None
    last_convergence_week = None

    while True:
        now    = now_utc()
        paris  = now_paris()
        is_weekend = paris.weekday() >= 5  # samedi=5, dimanche=6

        today = paris.date()

        # Brief matin à 8h (jours ouvrés uniquement)
        if paris.hour == 8 and not is_weekend and last_brief_date != today:
            await morning_brief()
            last_brief_date = today

        # Check-in à 21h (jours ouvrés uniquement) — juste la question, rien d'autre.
        # La réponse est stockée dans journal.json et servira au brief croisé du lendemain matin.
        if paris.hour == 21 and not is_weekend and last_checkin_date != today:
            await daily_checkin()
            last_checkin_date = today

        # Consolidation du journal à 7h le lendemain matin (1h avant le brief de 8h).
        # Les fondateurs peuvent répondre au check-in toute la nuit.
        if (
            checkin_state
            and not checkin_state.get("consolidated")
            and paris.hour == CHECKIN_CONSOLIDATION_HOUR
            and paris.date().isoformat() != checkin_state.get("date")
        ):
            try:
                await consolidate_checkin()
                awaiting_journal.clear()
                _save_checkin_state()
            except Exception as e:
                print(f"[checkin] Erreur consolidation : {e}")

        # Résumé hebdo dimanche à 10h (décalé pour éviter collision avec check-in 21h)
        week_num = paris.isocalendar()[1]
        if paris.weekday() == 6 and paris.hour == 10 and last_summary_week != week_num:
            await weekly_summary()
            last_summary_week = week_num

        # Détection convergence dimanche 19h (avant le digest 20h).
        # Le texte est broadcasté aux deux fondateurs ET passé au digest.
        if paris.weekday() == 6 and paris.hour == 19 and last_convergence_week != week_num:
            try:
                conv_text = await detect_convergence()
            except Exception as e:
                print(f"[convergence] Erreur : {e}")
                conv_text = ""
            if conv_text and "Aucune convergence" not in conv_text:
                try:
                    await broadcast(f"🔗 Convergence de la semaine :\n\n{conv_text}")
                except Exception as e:
                    print(f"[convergence] Broadcast erreur : {e}")
                global _pending_convergence_text
                _pending_convergence_text = conv_text
            last_convergence_week = week_num

        # Digest hebdo dimanche à 20h + nettoyage daily_updates
        if paris.weekday() == 6 and paris.hour == 20 and last_digest_week != week_num:
            try:
                await generate_weekly_digest(convergence_text=_pending_convergence_text)
            except Exception as e:
                print(f"[digest] Erreur digest hebdo : {e}")
            _pending_convergence_text = ""  # consommé
            try:
                cleanup_old_daily_updates(days=7)
            except Exception as e:
                print(f"[daily_update] Erreur nettoyage : {e}")
            last_digest_week = week_num

        # Digest mensuel — le 1er du mois à 9h
        current_month = (paris.year, paris.month)
        if paris.day == 1 and paris.hour == 9 and last_monthly_digest != current_month:
            try:
                await generate_monthly_digest()
            except Exception as e:
                print(f"[digest] Erreur digest mensuel : {e}")
            last_monthly_digest = current_month

        # Email hebdo profs — vendredi 18h
        if paris.weekday() == 4 and paris.hour == 18 and last_prof_email_week != week_num:
            try:
                await send_weekly_prof_email()
            except Exception as e:
                print(f"[email] Erreur rapport hebdo : {e}")
            last_prof_email_week = week_num

        await asyncio.sleep(60)


# ─── COMMANDES ────────────────────────────────────────────────────────────────

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update): return
    stats = get_vao_stats()
    if "error" in stats:
        await safe_reply(update, f"❌ API indisponible : {esc(stats['error'])}")
        return
    lines = ["📊 *Stats VAO*\n"]
    for k, v in stats.items():
        if isinstance(v, dict):
            lines.append(f"• *{esc(str(k))}* :")
            for sk, sv in v.items():
                lines.append(f"  — {esc(str(sk))} : {esc(str(sv))}")
        else:
            lines.append(f"• *{esc(str(k))}* : {esc(str(v))}")

    # Coût estimé (Haiku : $1/M input + $5/M output → ~$0.000001/token moyen)
    costs = load_costs()
    tokens_month = costs.get("tokens_month", 0)
    cost_usd     = round(tokens_month * 0.000001, 4)
    tokens_today = costs.get("tokens_today", 0)
    extractions  = costs.get("extractions_today", 0)
    lines.append(
        f"\n💰 *Coût estimé mois* : ~${cost_usd} "
        f"({tokens_month:,} tokens)\n"
        f"  Aujourd'hui : {tokens_today:,} tokens | {extractions}/{EXTRACT_DAILY_LIMIT} extractions"
    )
    await safe_reply(update, "\n".join(lines))


async def cmd_start_scraper(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update): return
    p = running_procs.get("scraper")
    if p and p.poll() is None:
        await safe_reply(update, "⚠️ Scraper déjà en cours.", markdown=False)
        return
    try:
        proc = launch_stage(
            "scraper",
            ["/opt/geo-leaad-fr-landscaping/.venv/bin/python",
             "/opt/geo-leaad-fr-landscaping/scripts/scheduler.py"],
        )
        running_procs["scraper"] = proc
        _record_stage_started("scraper")
        alert = load_json(ALERT_FILE, {})
        alert["last_scraper_active"] = now_utc().isoformat()
        save_json(ALERT_FILE, alert)
        await safe_reply(update, f"✅ Scraper lancé (PID {proc.pid})", markdown=False)
    except Exception as e:
        await safe_reply(update, f"❌ {e}", markdown=False)


async def cmd_stop_scraper(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update): return
    p = running_procs.get("scraper")
    if p and p.poll() is None:
        p.terminate()
        await safe_reply(update, "🛑 Scraper arrêté.", markdown=False)
    else:
        await safe_reply(update, "Aucun scraper en cours.", markdown=False)


async def cmd_start_enrich(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update): return
    p = running_procs.get("enrich")
    if p and p.poll() is None:
        await safe_reply(update, "⚠️ Enrichissement déjà en cours.", markdown=False)
        return
    try:
        # launch_stage("enrich", ...) passe par enrich.service (systemd) — découplé
        # du bot, log dans /opt/geo-leaad-fr-landscaping/logs/enrich.log.
        proc = launch_stage("enrich", [])
        running_procs["enrich"] = proc
        _record_stage_started("enrich")
        await safe_reply(update, f"✅ Enrichissement lancé via systemd ({proc.pid})", markdown=False)
    except Exception as e:
        await safe_reply(update, f"❌ {e}", markdown=False)


async def cmd_stop_enrich(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update): return
    p = running_procs.get("enrich")
    if p and p.poll() is None:
        p.terminate()
        await safe_reply(update, "🛑 Enrichissement arrêté.", markdown=False)
    else:
        await safe_reply(update, "Aucun enrichissement en cours.", markdown=False)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update): return
    lines = ["⚙️ *Status agents*\n"]
    for name, p in running_procs.items():
        st = "🟢 actif" if p.poll() is None else "🔴 arrêté"
        lines.append(f"• *{esc(name)}* : {st} (PID {p.pid})")
    if not running_procs:
        lines.append("Aucun agent actif.")
    lines.append(f"\n💾 Disque : {get_disk_pct()}%")
    ram = get_ram_free_pct()
    if ram is not None:
        lines.append(f"🧠 RAM libre : {ram}%")
    business = load_business()
    if business.get("derniere_maj"):
        lines.append(f"\n📊 Business (maj {business['derniere_maj'][:10]}) : {business.get('leads_total')} leads")
    await safe_reply(update, "\n".join(lines))


async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update): return
    logs = tail_logs(20)
    await safe_reply(update, f"```\n{logs}\n```")


async def cmd_ingest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update): return

    # Capturer le chat_id pour répondre UNIQUEMENT à l'initiateur (pas de broadcast)
    chat_id = str(update.effective_chat.id)
    kb_before = len(load_kb() or [])
    await safe_reply(update, f"📚 Ingestion en cours… ({kb_before} chunks en base)\nJe te préviens quand c'est terminé.", markdown=False)

    async def _run():
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                ["/opt/openclaw/venv/bin/python", "/opt/openclaw/scripts/ingest_docs.py"],
                capture_output=True, text=True,
            )
            out = result.stdout.strip() or result.stderr.strip() or "Terminé sans sortie."
            _invalidate_kb_cache()  # le script externe a réécrit knowledge_base.json
            kb_after = len(load_kb() or [])
            added    = kb_after - kb_before
            status   = f"✅ +{added} chunks" if added > 0 else "✅ Rien de nouveau"
            await send(chat_id, f"{status} — {kb_after} chunks au total\n```\n{out}\n```")
        except Exception as e:
            await send(chat_id, f"❌ Erreur ingestion : {e}")

    asyncio.create_task(_run())


async def cmd_brief(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update): return
    recipient = founder_name(update.effective_chat.id)
    msg = await _generate_brief_text(recipient=recipient)
    await safe_reply(update, msg)


# ─── INGESTION DOCUMENT TELEGRAM ──────────────────────────────────────────────

SUPPORTED_MIME = {"application/pdf", "text/plain"}
SUPPORTED_EXT  = {".pdf", ".txt"}

# Détecte un nom de fichier qui ressemble à un transcript d'entretien prospect.
# Si match, on lance une analyse automatique (douleurs, objections, signaux d'achat).
TRANSCRIPT_NAME_RE = re.compile(
    r'(transcript|interview|entretien|appel|call|\bP\d+)',
    re.IGNORECASE,
)


async def _analyze_transcript(text: str, founder: str, update: Update):
    """Analyse Sonnet d'un transcript d'entretien prospect, push insights au
    fondateur qui a envoyé le fichier + insert dans memories(apprentissage)."""
    if not text or not text.strip():
        return
    # Borner pour éviter un appel énorme (Sonnet gère bien 30k chars en input).
    snippet = text[:30000]
    prompt = (
        "Voici le transcript d'un entretien de découverte pour le projet VAO. "
        "Analyse et extrais :\n"
        "- Les points de douleur exprimés par le prospect\n"
        "- Les objections ou réticences\n"
        "- Les signaux d'achat (intérêt, questions sur le prix, demande de démo)\n"
        "- Les informations clés sur le prospect (taille d'entreprise, outils actuels, "
        "volume de devis)\n"
        "- Une note globale de qualification (froid / tiède / chaud / très chaud)\n"
        "Sois factuel et concis.\n\n"
        f"Transcript :\n\"\"\"\n{snippet}\n\"\"\""
    )
    try:
        resp = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1500,
            timeout=ANTHROPIC_TIMEOUT,
            messages=[{"role": "user", "content": prompt}],
        )
        analysis = resp.content[0].text.strip()
    except Exception as e:
        print(f"[transcript] Erreur analyse : {type(e).__name__}: {e}")
        await safe_reply(update, f"⚠️ Transcript ingéré, mais analyse impossible : {e}", markdown=False)
        return

    await safe_reply(update, f"📊 *Analyse du transcript*\n\n{analysis}")
    add_memory(
        category="apprentissage",
        content=analysis,
        source="transcript",
        founder=founder,
    )

def _extract_text_file(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        try:
            import pdfplumber
            parts = []
            with pdfplumber.open(path) as pdf:
                for page in pdf.pages:
                    t = page.extract_text()
                    if t:
                        parts.append(t)
            return "\n".join(parts)
        except Exception:
            pass  # PDF invalide — on tente en texte brut ci-dessous
    for enc in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return path.read_text(encoding=enc)
        except (UnicodeDecodeError, ValueError):
            continue
    return ""

def _make_chunks(text: str, source: str, chunk_size: int = 500, overlap: int = 50) -> list[dict]:
    words = text.split()
    chunks, start, idx = [], 0, 0
    while start < len(words):
        end   = start + chunk_size
        chunk = " ".join(words[start:end])
        chunks.append({
            "id":      f"{source}_{idx}",
            "source":  source,
            "content": chunk,
            "words":   min(chunk_size, len(words) - start),
        })
        idx   += 1
        start  = end - overlap
    return chunks

def _ingest_file(file_path: Path) -> tuple[int, bool]:
    """Ingère un fichier dans la KB. Retourne (nb_chunks_ajoutés, déjà_connu)."""
    source = file_path.stem
    kb = load_kb() or []
    if source in {c["source"] for c in kb}:
        return 0, True
    text = _extract_text_file(file_path)
    if not text.strip():
        return 0, False
    new_chunks = _make_chunks(text, source)
    kb.extend(new_chunks)
    KB_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(KB_FILE, "w", encoding="utf-8") as f:
        json.dump(kb, f, indent=2, ensure_ascii=False)
    _invalidate_kb_cache()
    return len(new_chunks), False


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update): return

    doc  = update.message.document
    mime = doc.mime_type or ""
    name = doc.file_name or ""
    ext  = Path(name).suffix.lower()

    if mime not in SUPPORTED_MIME and ext not in SUPPORTED_EXT:
        await safe_reply(update, "⚠️ Format non supporté. Envoie un PDF ou un fichier texte (.txt).", markdown=False)
        return

    # Nom de fichier sécurisé — on préserve l'extension réelle
    default_ext = ext if ext in SUPPORTED_EXT else (".pdf" if "pdf" in mime else ".txt")
    raw_name = doc.file_name or f"doc_{doc.file_id}{default_ext}"
    safe_name = re.sub(r'[^\w\s\-.]', '_', raw_name).strip()
    if Path(safe_name).suffix.lower() not in SUPPORTED_EXT:
        safe_name += default_ext

    dest = Path("/opt/openclaw/docs") / safe_name
    dest.parent.mkdir(parents=True, exist_ok=True)

    await safe_reply(update, f"📥 Réception de *{esc(raw_name)}*…")

    try:
        tg_file = await context.bot.get_file(doc.file_id)
        await tg_file.download_to_drive(str(dest))
    except Exception as e:
        await safe_reply(update, f"❌ Erreur téléchargement : {e}", markdown=False)
        return

    try:
        added, already_known = await asyncio.to_thread(_ingest_file, dest)
    except Exception as e:
        await safe_reply(update, f"❌ Erreur ingestion : {e}", markdown=False)
        return

    if already_known:
        await safe_reply(update, f"ℹ️ *{esc(raw_name)}* est déjà dans la knowledge base.")
        return

    if added == 0:
        await safe_reply(update, f"⚠️ Fichier reçu mais aucun texte extrait (*{esc(raw_name)}*).", markdown=False)
        return

    kb_total = len(load_kb() or [])
    await safe_reply(
        update,
        f"✅ *{esc(raw_name)}* ingéré — *{added} chunks* ajoutés\n"
        f"Knowledge base : {kb_total} chunks au total"
    )

    # Analyse automatique si le fichier ressemble à un transcript d'entretien.
    if TRANSCRIPT_NAME_RE.search(raw_name):
        founder = founder_name(update.effective_chat.id)
        try:
            text = await asyncio.to_thread(_extract_text_file, dest)
        except Exception as e:
            print(f"[transcript] Erreur extraction texte : {e}")
            return
        if text and text.strip():
            asyncio.create_task(_analyze_transcript(text, founder, update))


async def cmd_memoire(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update): return
    all_mem  = load_all_memories()
    kb       = load_kb()
    journal  = load_json(JOURNAL_FILE, [])

    parts = ["🧠 *Ce que je sais sur VAO*\n"]

    # Grouper par catégorie
    by_cat: dict[str, list[dict]] = {}
    for m in all_mem:
        by_cat.setdefault(m.get("category", "autre"), []).append(m)

    # Décisions
    decisions = by_cat.get("decision", [])
    if decisions:
        parts.append(f"\n*Décisions ({len(decisions)}) — 5 dernières :*")
        for d in decisions[:5]:
            parts.append(f"  • [{d.get('created_at','')[:10]}] {esc(d.get('content',''))}")
    else:
        parts.append("\n*Décisions :* aucune mémorisée")

    # Apprentissages
    appren = by_cat.get("apprentissage", [])
    if appren:
        parts.append(f"\n*Apprentissages ({len(appren)}) — 5 derniers :*")
        for a in appren[:5]:
            parts.append(f"  • [{a.get('created_at','')[:10]}] {esc(a.get('content',''))}")

    # Erreurs
    erreurs = by_cat.get("erreur", [])
    if erreurs:
        parts.append(f"\n*Erreurs connues ({len(erreurs)}) — 3 dernières :*")
        for e in erreurs[:3]:
            parts.append(f"  • [{e.get('created_at','')[:10]}] {esc(e.get('content',''))}")

    # Total mémoires
    parts.append(f"\n*Mémoires Supabase :* {len(all_mem)} entrée(s) au total")

    # Journal
    parts.append(f"*Journal :* {len(journal)} entrée(s) au total")

    # KB
    if kb:
        sources = list({c["source"] for c in kb})
        parts.append(f"*Knowledge base :* {len(kb)} chunks — {len(sources)} PDF(s)")
        if sources:
            parts.append("  Sources : " + ", ".join(sorted(sources)[:5]))
    else:
        parts.append("*Knowledge base :* vide (lance /ingest pour charger les PDFs)")

    await safe_reply(update, "\n".join(parts))


async def cmd_principes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/principes — Affiche, ajoute ou supprime un principe fondateur."""
    if not authorized(update): return
    args = (update.message.text or "").replace("/principes", "").strip()
    principles = load_principles()

    # Ajout
    if args.lower().startswith("ajoute"):
        new_p = re.sub(r'^ajoute\s*:\s*', '', args, flags=re.IGNORECASE).strip()
        if not new_p:
            await safe_reply(update, "Usage : /principes ajoute : ton nouveau principe")
            return
        principles.append(new_p)
        save_principles(principles)
        await safe_reply(update, f"✅ Principe ajouté (#{len(principles)}) : {esc(new_p)}")
        return

    # Suppression
    match = re.match(r'supprime\s+(?:le\s+)?(\d+)', args, re.IGNORECASE)
    if match:
        idx = int(match.group(1))
        if 1 <= idx <= len(principles):
            removed = principles.pop(idx - 1)
            save_principles(principles)
            await safe_reply(update, f"✅ Principe #{idx} supprimé : {esc(removed)}")
        else:
            await safe_reply(update, f"❌ Numéro invalide (1-{len(principles)})")
        return

    # Affichage
    if not principles:
        await safe_reply(update,
            "📜 *Principes fondateurs*\n\nAucun principe défini.\n"
            "Utilise `/principes ajoute : ton principe` pour en ajouter.")
        return
    lines = ["📜 *Principes fondateurs*\n"]
    for i, p in enumerate(principles, 1):
        lines.append(f"{i}. {esc(p)}")
    lines.append(f"\n_/principes ajoute : ... pour ajouter_")
    lines.append(f"_/principes supprime N pour retirer_")
    await safe_reply(update, "\n".join(lines))


async def cmd_test_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/test_email — envoie un email de test à QUENTIN_EMAIL via Resend."""
    if not authorized(update): return
    if not RESEND_API_KEY:
        await safe_reply(
            update,
            "❌ RESEND_API_KEY absent du .env — vérifie /opt/openclaw/.env",
            markdown=False,
        )
        return
    if not QUENTIN_EMAIL:
        await safe_reply(
            update,
            "❌ QUENTIN_EMAIL absent du .env — vérifie /opt/openclaw/.env",
            markdown=False,
        )
        return

    await safe_reply(
        update,
        f"📧 Envoi d'un email de test à {QUENTIN_EMAIL} (from: {RESEND_FROM})…",
        markdown=False,
    )
    paris = now_paris()
    subject = f"Test {BOT_NAME} — {paris.strftime('%d/%m %H:%M')}"
    body = (
        f"Ceci est un email de test envoyé par {BOT_NAME} via Resend.\n\n"
        f"Heure : {paris.isoformat()}\n"
        f"From : {RESEND_FROM}\n"
        f"To   : {QUENTIN_EMAIL}\n\n"
        "Si tu reçois cet email, la chaîne Resend est opérationnelle."
    )
    result = send_email_resend(QUENTIN_EMAIL, subject, body)
    if result["ok"]:
        await safe_reply(
            update,
            f"✅ Email de test envoyé (id: {result.get('id', '?')})\n"
            f"Vérifie ta boîte ({QUENTIN_EMAIL}) — pense aussi aux spams.",
            markdown=False,
        )
    else:
        await safe_reply(
            update,
            f"❌ Échec envoi : {result['error']}\n\n"
            f"Pistes :\n"
            f"• Domaine {RESEND_FROM.split('@')[-1]} non vérifié sur Resend ?\n"
            f"• Clé API expirée/invalide ?\n"
            f"• Quota Resend dépassé ?",
            markdown=False,
        )


async def cmd_push(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Analyse les 10 derniers échanges (user + assistant) et pousse les actions dans le Trello."""
    if not authorized(update): return
    chat_id = str(update.effective_chat.id)
    history = get_history(chat_id)
    if not history:
        await safe_reply(update, "Aucun historique de conversation à analyser.", markdown=False)
        return

    last_msgs = history[-10:]
    combined = "\n\n".join(
        f"[{m['role']}] {m['content']}" for m in last_msgs
    )

    await safe_reply(
        update,
        f"🔍 Analyse des {len(last_msgs)} derniers messages…",
        markdown=False,
    )

    tasks = await extract_tasks_list(combined, is_conversation=True)
    if not tasks:
        await safe_reply(
            update,
            "Je ne vois pas d'actions concrètes dans notre conversation récente. "
            "Dis-moi explicitement quoi ajouter.",
            markdown=False,
        )
        return

    result = await insert_tasks_supabase(tasks)
    if result.get("error"):
        await safe_reply(update, f"Erreur insertion : {result['error']}")
        return

    inserted = result.get("inserted", [])
    msg = format_inserted_tasks(
        inserted,
        f"✅ *{len(inserted)} tâche(s) ajoutée(s) au Trello :*",
    )
    await safe_reply(update, msg)


# ─── MENU INTERACTIF ─────────────────────────────────────────────────────────
# Routing callback_data → fonction de commande. Les fonctions reçoivent
# (update, context) ; safe_reply gère update.effective_message → OK pour
# callback comme pour commande classique.
def _menu_routes():
    return {
        # Dashboard
        "menu_stats":         cmd_stats,
        "menu_status":        cmd_status,
        "menu_logs":          cmd_logs,
        # Pipeline
        "menu_start_scraper": cmd_start_scraper,
        "menu_stop_scraper":  cmd_stop_scraper,
        "menu_start_enrich":  cmd_start_enrich,
        "menu_stop_enrich":   cmd_stop_enrich,
        "menu_ingest":        cmd_ingest,
        # Outreach
        "menu_test_email":    cmd_test_email,
        # Agent
        "menu_brief":         cmd_brief,
        "menu_memoire":       cmd_memoire,
        "menu_push":          cmd_push,
        # Système
        "menu_aide":          None,  # rendu plus bas : ré-affiche le menu
    }


def build_menu_markup() -> InlineKeyboardMarkup:
    B = InlineKeyboardButton
    # Un bouton "noop" sert de header de catégorie (cliquable mais sans effet).
    H = lambda label: B(label, callback_data="menu_noop")
    rows = [
        [H("—— 📊 Dashboard ——")],
        [B("📊 Stats",        callback_data="menu_stats"),
         B("⚙️ Status",       callback_data="menu_status")],
        [B("📋 Logs",         callback_data="menu_logs")],

        [H("—— 🔧 Pipeline ——")],
        [B("▶️ Scraper",      callback_data="menu_start_scraper"),
         B("⏹ Stop scraper",  callback_data="menu_stop_scraper")],
        [B("▶️ Enrich",       callback_data="menu_start_enrich"),
         B("⏹ Stop enrich",   callback_data="menu_stop_enrich")],
        [B("📥 Ingest KB",    callback_data="menu_ingest")],

        [H("—— 📬 Outreach ——")],
        [B("📧 Test email",   callback_data="menu_test_email")],

        [H("—— 🧠 Agent ——")],
        [B("☀️ Brief",        callback_data="menu_brief"),
         B("🧠 Mémoire",      callback_data="menu_memoire")],
        [B("📌 Push actions", callback_data="menu_push")],

        [H("—— ⚙️ Système ——")],
        [B("❓ Aide",          callback_data="menu_aide")],
    ]
    return InlineKeyboardMarkup(rows)


MENU_TITLE = (
    f"🤖 *{BOT_NAME} — Menu*\n\n"
    "Clique sur une action. Tu peux aussi taper la commande directement."
)


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update): return
    await update.effective_message.reply_text(
        MENU_TITLE,
        reply_markup=build_menu_markup(),
        parse_mode="Markdown",
    )


async def cmd_aide(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /aide affiche le même menu interactif que /menu.
    await cmd_menu(update, context)


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()  # acquitter immédiatement (évite le sablier Telegram)

    if not authorized(update):
        return

    data = query.data or ""
    if data == "menu_noop":
        return
    if data == "menu_aide":
        # Ré-affiche le menu pour rester cohérent avec /aide.
        await cmd_menu(update, context)
        return

    handler = _menu_routes().get(data)
    if handler is None:
        return
    try:
        await handler(update, context)
    except Exception as e:
        print(f"[menu] Erreur route {data} : {e}")
        await query.message.reply_text(f"❌ Erreur : {e}")


# ─── MESSAGES LIBRES ──────────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Point d'entrée des messages texte. Debounce de 2s pour regrouper les
    messages découpés par Telegram avant de traiter."""
    if not authorized(update): return
    chat_id = str(update.effective_chat.id)
    text    = update.message.text
    if not text:
        return

    # Ajouter au buffer
    _msg_buffer.setdefault(chat_id, []).append(text)
    _msg_buffer_update[chat_id] = update  # garder le dernier update pour reply

    # Annuler le timer précédent s'il existe
    prev = _msg_buffer_timer.get(chat_id)
    if prev and not prev.done():
        prev.cancel()

    # Afficher "typing" dès le premier fragment
    try:
        await update.effective_chat.send_action("typing")
    except Exception:
        pass

    # Lancer un nouveau timer
    async def _flush():
        await asyncio.sleep(_MSG_DEBOUNCE_SEC)
        texts  = _msg_buffer.pop(chat_id, [])
        latest = _msg_buffer_update.pop(chat_id, None)
        _msg_buffer_timer.pop(chat_id, None)
        if texts and latest:
            merged = "\n\n".join(texts)
            await _process_message(latest, context, merged)

    _msg_buffer_timer[chat_id] = asyncio.create_task(_flush())


async def _process_message(update: Update, context: ContextTypes.DEFAULT_TYPE,
                           text: str):
    """Traite un message (éventuellement fusionné par le debounce)."""
    chat_id     = str(update.effective_chat.id)
    # FOUNDERS est la source unique de vérité — le prénom Telegram est un fallback
    # (évite "Fondateur" dans le journal si Telegram ne renvoie pas de first_name).
    author_name = FOUNDERS.get(chat_id) or update.effective_user.first_name or "Fondateur"

    # Réponse au check-in quotidien — stockée, journal consolidé en différé
    if chat_id in awaiting_journal:
        awaiting_journal.discard(chat_id)
        analyse = await analyze_checkin(author_name, text)
        # On stocke dans checkin_state au lieu d'écrire directement dans le journal
        if checkin_state is not None and not checkin_state.get("consolidated"):
            checkin_state["responses"][chat_id] = {
                "author":      author_name,
                "text":        text,
                "analyse":     analyse,
                "received_at": now_utc().isoformat(),
            }
            _save_checkin_state()
        else:
            # Fallback : pas d'état actif → on écrit directement (cas réponse tardive)
            append_journal(chat_id, author_name, text, analyse)
            _save_checkin_state()
        # Extraction mémoire en arrière-plan
        asyncio.create_task(extract_and_save_memory(text, analyse, founder=author_name))
        # Stocker la réponse check-in dans daily_updates pour le brief croisé
        insert_daily_update(author_name, text, source="checkin")
        await safe_reply(update, analyse)
        return

    # Tâches : UNIQUEMENT si l'utilisateur le demande explicitement
    # ("ajoute une tâche", "mets ça dans le dashboard", etc.).
    # Le check-in du soir et /push restent les autres canaux d'ajout.
    inserted_tasks: list[dict] = []
    task_context = ""
    if is_explicit_task_request(text):
        try:
            # On lit l'historique récent : "ajoute ça au dashboard" fait souvent
            # référence à ce qui vient d'être discuté, pas au texte du déclencheur.
            recent = get_history(chat_id)[-8:]
            if recent:
                convo = "\n\n".join(
                    f"[{m['role']}] {m['content']}" for m in recent
                )
                convo += f"\n\n[user] {text}"
                tasks = await extract_tasks_list(convo, is_conversation=True)
            else:
                tasks = await extract_tasks_list(text)
            if tasks:
                result = await insert_tasks_supabase(tasks)
                inserted_tasks = result.get("inserted", [])
                if result.get("error"):
                    await safe_reply(update, f"Erreur insertion : {result['error']}")
            else:
                await safe_reply(
                    update,
                    "Je ne vois pas d'action concrète à ajouter. "
                    "Dis-moi explicitement quoi mettre dans le dashboard.",
                    markdown=False,
                )
                # Pas de tâche extraite → on a déjà répondu, on n'enchaîne pas Claude.
                add_to_history(chat_id, "user", text)
                add_to_history(chat_id, "assistant", "[aucune tâche détectée]")
                return
        except Exception as e:
            print(f"[task_explicit] Erreur : {e}")
        if inserted_tasks:
            task_lines = "\n".join(
                f"- {t['title']} → {t.get('assigned_to', 'Quentin')} ({t.get('tag', 'Admin')})"
                for t in inserted_tasks
            )
            task_context = (
                f"\n[Tâches déjà ajoutées au dashboard sur sa demande "
                f"(NE PAS les répéter, elles ont déjà été confirmées dans un "
                f"message séparé) :\n{task_lines}]\n"
            )
            confirmation_msg = format_inserted_tasks(
                inserted_tasks,
                f"✅ *{len(inserted_tasks)} tâche(s) ajoutée(s) au dashboard :*",
            )
            await safe_reply(update, confirmation_msg)

            # Si le message ne contient pas de question explicite EN PLUS de
            # la demande de tâche, la confirmation suffit : on ne génère pas
            # de réponse Claude derrière (sinon double message redondant).
            if not QUESTION_RE.search(text):
                add_to_history(chat_id, "user", text)
                add_to_history(chat_id, "assistant", confirmation_msg)
                return

    # Contexte VAO + RAG
    journal = load_json(JOURNAL_FILE, [])[-5:]
    journal_text = "\n".join(
        f"- [{e['date'][:10]}] {e.get('auteur', e.get('author_name','?'))}: {e.get('contenu', e.get('content',''))}"
        for e in journal
    ) or "Aucune entrée récente."

    # Stats VAO injectées uniquement si la question s'y rapporte (sinon Claude a
    # ordre de ne pas en parler — autant ne pas les charger en contexte).
    stats_text = ""
    if STATS_TRIGGER_RE.search(text):
        stats = get_vao_stats()
        stats_text = (
            json.dumps(stats, ensure_ascii=False)
            if "error" not in stats
            else f"API indisponible ({stats['error']})"
        )

    rag_chunks = search_kb(text, top_k=3)
    rag_section = ""
    if rag_chunks:
        rag_block   = "\n\n---\n".join(rag_chunks)
        rag_section = (
            f"\nExtraits de cours pertinents :\n---\n{rag_block}\n---\n"
            "Si tu utilises ces extraits, commence par \"Selon tes cours : \".\n"
        )

    # Construction des messages avec historique glissant
    history = get_history(chat_id)

    # Message utilisateur enrichi (contexte injecté seulement dans le dernier message)
    stats_block = f"Stats VAO : {stats_text}\n" if stats_text else ""
    user_content = (
        f"{stats_block}"
        f"Journal récent :\n{journal_text}\n"
        f"{rag_section}"
        f"{task_context}"
        f"Utilisateur : {author_name}\n\n"
        f"{text}"
    )

    # Si historique existe, on injecte le contexte seulement dans le nouveau message
    messages = history + [{"role": "user", "content": user_content}]

    # Réponse Claude avec continuation automatique si stop_reason=="max_tokens".
    # 2 itérations max → 8 000 tokens out plafond. Au-delà, c'est trop long pour Telegram.
    reply = ""
    try:
        sys_prompt = build_system_prompt(user_query=text)
        cur_messages = list(messages)
        for iteration in range(2):
            resp = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=4000,
                timeout=ANTHROPIC_TIMEOUT,
                system=sys_prompt,
                messages=cur_messages,
            )
            chunk = resp.content[0].text if resp.content else ""
            reply += chunk
            stop = getattr(resp, "stop_reason", None)
            if stop != "max_tokens":
                break
            print(f"[claude] Continuation auto (iter {iteration + 1}) — stop_reason=max_tokens, len={len(reply)}")
            # On rappelle Claude en lui passant la réponse partielle pour qu'il la termine
            cur_messages = cur_messages + [
                {"role": "assistant", "content": chunk},
                {"role": "user",      "content": "Continue exactement où tu t'es arrêté, sans répéter ce que tu viens d'écrire."},
            ]
    except anthropic.APITimeoutError as e:
        print(f"[claude] Timeout après {ANTHROPIC_TIMEOUT}s : {e}")
        if not reply:
            reply = "Désolé, je mets trop de temps à répondre. Réessaie dans quelques secondes."
        else:
            reply += "\n\n[interrompu : trop long]"
    except Exception as e:
        print(f"[claude] Erreur : {type(e).__name__}: {e}")
        if not reply:
            reply = f"Erreur Claude : {e}"
        else:
            reply += f"\n\n[continuation interrompue : {e}]"

    # Mise à jour de l'historique
    add_to_history(chat_id, "user", text)
    add_to_history(chat_id, "assistant", reply)

    # Extraction mémoire + daily update en arrière-plan (silencieuses)
    asyncio.create_task(extract_and_save_memory(text, reply, founder=author_name))
    asyncio.create_task(extract_daily_update(text, founder=author_name))

    # Transmission de message entre fondateurs
    transmit_match = re.search(r'\[TRANSMETTRE:(.*?)\]', reply, re.DOTALL)
    if transmit_match:
        msg_to_transmit = transmit_match.group(1).strip()
        if msg_to_transmit:
            # Trouver l'autre fondateur
            other_cids = [cid2 for cid2 in CHAT_IDS if cid2 != chat_id]
            for other_cid in other_cids:
                other_name = founder_name(other_cid)
                formatted_msg = f"💬 Message de {author_name} :\n\n{msg_to_transmit}"
                try:
                    await send(other_cid, formatted_msg)
                    insert_daily_update(author_name, msg_to_transmit, source="message_transmis")
                    print(f"[transmit] Message de {author_name} → {other_name}")
                except Exception as e:
                    print(f"[transmit] Erreur envoi à {other_name} : {e}")
            # Retirer la balise de la réponse affichée à l'expéditeur
            reply_clean = re.sub(r'\[TRANSMETTRE:.*?\]', '', reply, flags=re.DOTALL).strip()
            other_names = [founder_name(c) for c in other_cids]
            confirm = f"\n\n✅ Message transmis à {', '.join(other_names)}."
            reply = (reply_clean + confirm) if reply_clean else f"Message transmis à {', '.join(other_names)}."

    await safe_reply(update, reply)


# ─── STARTUP ──────────────────────────────────────────────────────────────────

def auto_ingest_if_empty():
    """Lance l'ingestion des PDFs si la knowledge base est vide."""
    kb = load_kb()
    if kb and len(kb) > 0:
        return
    docs = list((BASE / "docs").rglob("*.pdf"))
    if not docs:
        return
    print(f"[startup] Knowledge base vide — ingestion de {len(docs)} PDF(s)...")
    try:
        subprocess.run(
            ["/opt/openclaw/venv/bin/python", "/opt/openclaw/scripts/ingest_docs.py"],
            timeout=180,
        )
        print("[startup] Ingestion terminée.")
    except Exception as e:
        print(f"[startup] Ingestion échouée : {e}")

def init_memory_files():
    """Crée les fichiers mémoire manquants avec leur structure par défaut."""
    if not BUSINESS_FILE.exists():
        save_json(BUSINESS_FILE, default_business())
        print("[startup] business.json créé.")


# ─── MAIN ─────────────────────────────────────────────────────────────────────

async def main():
    init_memory_files()
    auto_ingest_if_empty()

    # Restauration de l'historique de conversation (persistant à travers les restarts)
    load_history_from_disk()
    # Restauration du check-in en cours (réponses 21h ↔ 7h, awaiting_journal)
    _load_checkin_state()

    # Refresh business au démarrage
    try:
        refresh_business()
    except Exception:
        pass

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("stats",          cmd_stats))
    app.add_handler(CommandHandler("start_scraper",  cmd_start_scraper))
    app.add_handler(CommandHandler("stop_scraper",   cmd_stop_scraper))
    app.add_handler(CommandHandler("start_enrich",   cmd_start_enrich))
    app.add_handler(CommandHandler("stop_enrich",    cmd_stop_enrich))
    app.add_handler(CommandHandler("status",         cmd_status))
    app.add_handler(CommandHandler("logs",           cmd_logs))
    app.add_handler(CommandHandler("ingest",         cmd_ingest))
    app.add_handler(CommandHandler("brief",          cmd_brief))
    app.add_handler(CommandHandler("memoire",        cmd_memoire))
    app.add_handler(CommandHandler("push",           cmd_push))
    app.add_handler(CommandHandler("test_email",     cmd_test_email))
    app.add_handler(CommandHandler("principes",      cmd_principes))
    app.add_handler(CommandHandler("aide",           cmd_aide))
    app.add_handler(CommandHandler("menu",           cmd_menu))
    # Routing des boutons du menu interactif (pattern ^menu_).
    app.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^menu_"))
    app.add_handler(MessageHandler(filters.Document.PDF | filters.Document.TXT, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    # Message de démarrage : uniquement à Quentin (CHAT_ID_1), pas à Laurie.
    startup_text = f"🤖 *{BOT_NAME} en ligne*"
    startup_markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("📋 Menu", callback_data="menu_aide")]]
    )
    if CHAT_IDS:
        cid = CHAT_IDS[0]
        try:
            await bot().send_message(
                chat_id=cid,
                text=startup_text,
                parse_mode="Markdown",
                reply_markup=startup_markup,
            )
        except Exception as e:
            print(f"[startup] Erreur envoi menu à {cid} : {e}")

    asyncio.create_task(start_crm_watcher())

    await scheduler_loop()


if __name__ == "__main__":
    asyncio.run(main())
