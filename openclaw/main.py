import json, asyncio, datetime, subprocess, shutil, requests, re, time
from pathlib import Path
from collections import deque
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
    JOURNAL_FILE, ALERT_FILE, KB_FILE, LONG_TERM_FILE, BUSINESS_FILE, COSTS_FILE,
    HISTORY_FILE,
    EXTRACT_DAILY_LIMIT, TOKEN_DAILY_LIMIT, HISTORY_LIMIT,
    CHECKIN_DELAY_AFTER_LAST, CHECKIN_HARD_DEADLINE,
    API_VAO_URL, API_VAO_KEY,
    SUPABASE_URL, SUPABASE_KEY,
    SUPABASE_LOVABLE_URL, SUPABASE_LOVABLE_KEY,
    RESEND_API_KEY, RESEND_FROM, QUENTIN_EMAIL,
    FOUNDERS, founder_name,
    BOT_NAME, BOT_SUPABASE_AUTHOR,
)
from autonomous_loop import (
    autonomous_loop_tick,
    daily_autonomous_report,
    cmd_autonome,
    cmd_decisions,
    cmd_plan,
    _record_stage_started,
)
from crm_watcher import start_crm_watcher

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

# Alias historique : toutes les ancres `esc(...)` du fichier mappent sur strip_markdown.
esc = strip_markdown

def default_long_term() -> dict:
    return {
        "decisions": [],
        "erreurs": [],
        "apprentissages": [],
        "contexte_vao": {
            "produit": "SaaS devis paysagistes",
            "stack": "FastAPI + Supabase + Hetzner",
            "leads_total_initial": 21000,
        },
    }

def default_business() -> dict:
    return {
        "leads_total": 0,
        "enrichis": 0,
        "nettoyes": 0,
        "statuts": {},
        "derniere_maj": "",
    }


# ─── MÉMOIRE LONG TERME ───────────────────────────────────────────────────────

def load_long_term() -> dict:
    return load_json(LONG_TERM_FILE, default_long_term())

def save_long_term(lt: dict):
    save_json(LONG_TERM_FILE, lt)

def add_to_long_term(category: str, content: str):
    """Ajoute une entrée dans decisions / erreurs / apprentissages."""
    lt = load_long_term()
    if category not in lt:
        lt[category] = []
    lt[category].append({
        "date": now_utc().isoformat(),
        "contenu": content,
    })
    # Garde les 100 dernières entrées par catégorie
    if len(lt[category]) > 100:
        lt[category] = lt[category][-100:]
    save_long_term(lt)


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

def load_kb() -> list | None:
    if not KB_FILE.exists():
        return None
    try:
        with open(KB_FILE, encoding="utf-8") as f:
            kb = json.load(f)
        return kb if isinstance(kb, list) else None
    except Exception:
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

_sys_prompt_cache: tuple[float, str] | None = None
_SYS_PROMPT_TTL = 60  # secondes — recharge KB/long_term/business au max 1×/min

def build_system_prompt() -> str:
    global _sys_prompt_cache
    mono = time.monotonic()
    if _sys_prompt_cache and mono - _sys_prompt_cache[0] < _SYS_PROMPT_TTL:
        return _sys_prompt_cache[1]

    base = (BASE / "system_prompt.txt").read_text(encoding="utf-8")

    lt       = load_long_term()
    business = load_business()

    parts = [base, "\n\n--- MÉMOIRE LONG TERME ---"]

    # Décisions récentes
    decisions = lt.get("decisions", [])[-10:]
    if decisions:
        parts.append("Décisions prises :")
        for d in decisions:
            parts.append(f"  [{d.get('date','')[:10]}] {d.get('contenu','')}")

    # Apprentissages récents
    appren = lt.get("apprentissages", [])[-10:]
    if appren:
        parts.append("Apprentissages :")
        for a in appren:
            parts.append(f"  [{a.get('date','')[:10]}] {a.get('contenu','')}")

    # Erreurs connues
    erreurs = lt.get("erreurs", [])[-5:]
    if erreurs:
        parts.append("Erreurs passées à éviter :")
        for e in erreurs:
            parts.append(f"  [{e.get('date','')[:10]}] {e.get('contenu','')}")

    # Snapshot business — 3 lignes max
    if business.get("derniere_maj"):
        statuts = business.get("statuts", {})
        top_statuts = ", ".join(
            f"{k}={v}" for k, v in list(statuts.items())[:4]
        )
        parts.append("\n--- BUSINESS ---")
        parts.append(f"Leads : {business.get('leads_total')} | Enrichis : {business.get('enrichis')}")
        if top_statuts:
            parts.append(f"Statuts : {top_statuts}")
        parts.append(f"(màj {business.get('derniere_maj','')[:10]})")

    # Injection KB : sources disponibles + 3 chunks récents (sans recherche active)
    kb = load_kb()
    if kb:
        sources = sorted({c.get("source", "") for c in kb if c.get("source")})
        transcript_sources = [s for s in sources if "transcript" in s.lower() or re.match(r'P\d+', s)]
        other_sources = [s for s in sources if s not in transcript_sources]
        parts.append("\n--- BASE DE CONNAISSANCE (KB) ---")
        parts.append(f"Tu as accès à {len(kb)} chunks dans ta KB.")
        parts.append(f"TRANSCRIPTIONS DISPONIBLES ({len(transcript_sources)}) : {', '.join(transcript_sources)}")
        parts.append(f"Autres sources ({len(other_sources)}) : {', '.join(other_sources[:30])}")
        # Injecter des chunks de transcriptions en priorité
        transcript_chunks = [
            c for c in kb
            if re.match(r'P\d+', c.get("source", "")) or "transcript" in c.get("source", "").lower()
        ]
        inject_chunks = transcript_chunks[-3:] if transcript_chunks else kb[-3:]
        parts.append(f"3 chunks de transcriptions chargés automatiquement (sur {len(transcript_chunks)} disponibles) :")
        for chunk in inject_chunks:
            src = chunk.get("source", "?")
            parts.append(f"  [{src}] {chunk.get('content','')[:300]}")

    result = "\n".join(parts)
    _sys_prompt_cache = (mono, result)
    return result


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


async def extract_and_save_memory(user_text: str, ai_response: str):
    """
    Appel Haiku léger pour extraire décisions/apprentissages/erreurs.
    Garde-fous : message > 50 mots, cap journalier (EXTRACT_DAILY_LIMIT),
    cap tokens (TOKEN_DAILY_LIMIT).
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
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
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
        for category in ("decisions", "apprentissages", "erreurs"):
            for item in extracted.get(category, []):
                if item and len(item) > 10:
                    add_to_long_term(category, item)
    except Exception as e:
        # Ne jamais bloquer la conversation, mais laisser une trace pour debug.
        print(f"[memory_extract] Erreur : {type(e).__name__}: {e}")


# ─── ANALYSE CHECK-IN ─────────────────────────────────────────────────────────

async def analyze_checkin(author_name: str, content: str) -> str:
    """Claude analyse l'entrée de journal et retourne un commentaire court."""
    lt = load_long_term()
    decisions_recent = "\n".join(
        f"- {d.get('contenu','')}"
        for d in lt.get("decisions", [])[-5:]
    ) or "Aucune."

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

async def check_crashes():
    alert = load_json(ALERT_FILE, {})
    now   = now_utc()

    def should_alert(key: str) -> bool:
        last = alert.get(key)
        if not last:
            return True
        return (now - datetime.datetime.fromisoformat(last)).total_seconds() > 3600

    problems = []

    # API VAO
    try:
        r = requests.get(f"{API_VAO_URL}/stats", headers={"X-API-Key": API_VAO_KEY}, timeout=10)
        api_ok = r.status_code == 200
    except Exception:
        api_ok = False

    if not api_ok:
        if should_alert("api_down"):
            problems.append("🔴 *API VAO hors ligne* — `/stats` ne répond pas")
            alert["api_down"] = now.isoformat()
    else:
        alert.pop("api_down", None)

    # Disque
    disk = get_disk_pct()
    if disk > 90:
        if should_alert("disk_full"):
            problems.append(f"💾 *Disque critique* — {disk}% utilisé")
            alert["disk_full"] = now.isoformat()
    else:
        alert.pop("disk_full", None)

    # RAM
    ram = get_ram_free_pct()
    if ram is not None and ram < 10:
        if should_alert("ram_low"):
            problems.append(f"🧠 *RAM critique* — {ram}% disponible")
            alert["ram_low"] = now.isoformat()
    else:
        alert.pop("ram_low", None)

    save_json(ALERT_FILE, alert)
    if problems:
        await broadcast(f"⚠️ *Alerte {BOT_NAME}*\n\n" + "\n".join(problems))


async def check_proactive_alerts(stats: dict):
    """Analyse autonome : collecte les conditions et laisse Claude formuler le message."""
    # Pas d'alertes d'inactivité le week-end
    if now_paris().weekday() >= 5:
        return

    alert = load_json(ALERT_FILE, {})
    now   = now_utc()
    paris = now_paris()
    conditions: list[str] = []  # faits bruts à passer à Claude

    def should_alert(key: str, hours: int = 24) -> bool:
        last = alert.get(key)
        if not last:
            return True
        return (now - datetime.datetime.fromisoformat(last)).total_seconds() > hours * 3600

    # Aucun lead contacté depuis 7 jours
    statuts = stats.get("repartition_statut", {})
    contactes = statuts.get("contacte", 0)
    leads_total = stats.get("total_leads", stats.get("leads", 0))

    journal = load_json(JOURNAL_FILE, [])
    cutoff_7d = now - datetime.timedelta(days=7)
    recent_journal = [
        e for e in journal
        if datetime.datetime.fromisoformat(e["date"]) > cutoff_7d
    ]
    contact_mentions = any(
        any(kw in e.get("contenu", "").lower() for kw in ["contact", "appelé", "email", "prospection", "relance"])
        for e in recent_journal
    )

    if not contact_mentions and contactes == 0 and should_alert("no_contact_7d", hours=168):
        conditions.append(
            f"Aucun lead contacté cette semaine ({leads_total} leads en base, "
            f"aucune mention de prospection dans le journal)."
        )
        alert["no_contact_7d"] = now.isoformat()
    else:
        alert.pop("no_contact_7d", None)

    # Scraper inactif depuis 48h
    scraper_proc = running_procs.get("scraper")
    scraper_running = scraper_proc and scraper_proc.poll() is None
    last_scraper = alert.get("last_scraper_active")
    scraper_silent_h = 0
    if last_scraper:
        last_scraper_dt = datetime.datetime.fromisoformat(last_scraper)
        scraper_silent_h = (now - last_scraper_dt).total_seconds() / 3600

    if scraper_running:
        alert["last_scraper_active"] = now.isoformat()
        alert.pop("scraper_inactive_48h", None)
    else:
        scraper_exit_code = alert.get("last_scraper_exit_code")
        scraper_finished  = alert.get("last_scraper_finished")
        scraper_ended_ok  = (scraper_finished is not None and scraper_exit_code == 0)

        if not scraper_ended_ok and scraper_silent_h > 48 and last_scraper and should_alert("scraper_inactive_48h", hours=24):
            conditions.append(
                f"Scraper inactif depuis {int(scraper_silent_h)}h "
                f"(commande pour le relancer : /start_scraper)."
            )
            alert["scraper_inactive_48h"] = now.isoformat()
        elif scraper_ended_ok:
            alert.pop("scraper_inactive_48h", None)

    # Trajectoire hebdo : peu d'entrées journal en milieu de semaine
    weekday = paris.weekday()
    if weekday in (2, 3) and len(recent_journal) < 2 and should_alert("low_activity", hours=48):
        conditions.append(
            f"Peu d'activité journalisée cette semaine "
            f"({len(recent_journal)} entrée(s) en {weekday + 1} jour(s))."
        )
        alert["low_activity"] = now.isoformat()

    save_json(ALERT_FILE, alert)
    if not conditions:
        return

    # Laisser Claude formuler le message d'alerte (pas de template rigide)
    facts_block = "\n".join(f"- {c}" for c in conditions)
    prompt = (
        "Voici des signaux préoccupants détectés sur le projet VAO aujourd'hui :\n"
        f"{facts_block}\n\n"
        "Écris un message direct à Quentin pour l'alerter. "
        "Pas de mise en forme excessive, pas de titres, pas de listes à puces. "
        "Va droit au but et challenge si nécessaire."
    )
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system=build_system_prompt(),
            messages=[{"role": "user", "content": prompt}],
        )
        msg = resp.content[0].text.strip()
    except Exception:
        # Fallback minimal en cas de pépin Claude
        msg = "Quelques signaux à regarder :\n" + facts_block
    await broadcast(msg)


async def _generate_brief_text(trigger_alerts: bool = True, recipient: str = "Quentin") -> str:
    """Génère le texte du brief matin (pur, sans envoi) adressé à `recipient`.
    Si trigger_alerts=False, on saute l'envoi d'alertes proactives (utile quand
    un user force /brief et qu'on ne veut pas broadcaster d'alertes à tout le monde)."""
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

    # Alertes proactives en même temps que le brief (broadcast → seulement
    # quand le brief vient du scheduler, pas quand un user clique /brief).
    if trigger_alerts and "error" not in stats:
        await check_proactive_alerts(stats)

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
            system=build_system_prompt(),
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip()
    except Exception as e:
        return f"Brief du {today_str} — agents: {agents_text}. Stats: {stats_text}. (Claude indispo: {e})"


async def morning_brief():
    """Brief matin programmé — un brief personnalisé par fondateur + alertes (une seule fois)."""
    first = True
    for cid in CHAT_IDS:
        name = founder_name(cid)
        # trigger_alerts seulement sur le premier fondateur pour éviter le double broadcast
        msg = await _generate_brief_text(trigger_alerts=first, recipient=name)
        await send(cid, msg)
        first = False


async def daily_checkin():
    """Envoie la question check-in personnalisée à chaque fondateur."""
    global checkin_state
    today_iso = now_paris().date().isoformat()
    checkin_state = {
        "date":         today_iso,
        "sent_at":      now_utc().isoformat(),
        "responses":    {},
        "consolidated": False,
    }
    for cid in CHAT_IDS:
        name = founder_name(cid)
        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=80,
                system=build_system_prompt(),
                messages=[{"role": "user", "content": (
                    f"C'est l'heure du check-in du soir avec {name}, fondateur de VAO. "
                    f"Écris-lui UN message court adressé directement à {name} "
                    f"(commence par le saluer par son prénom) pour lui demander ce qu'il/elle "
                    f"a fait sur VAO aujourd'hui. Une ou deux phrases, naturel, tutoie-le/la."
                )}],
            )
            question = resp.content[0].text.strip()
        except Exception:
            question = f"Bonsoir {name}, qu'est-ce que t'as avancé sur VAO aujourd'hui ?"

        awaiting_journal.add(cid)
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
    plural = "s" if n > 1 else ""
    await broadcast(
        f"📒 Journal de la journée enregistré ({n} réponse{plural})."
    )
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
    lt       = load_long_term()
    business = load_business()

    if not week:
        await broadcast("Aucune entrée de journal cette semaine — pas de récap à faire.")
        return

    journal_text = "\n".join(
        f"- [{e['date'][:10]}] {e.get('auteur', e.get('author_name','?'))}: {e.get('contenu', e.get('content',''))}"
        for e in week
    )
    decisions_text = "\n".join(
        f"- {d.get('contenu','')}" for d in lt.get("decisions", [])[-10:]
    ) or "Aucune."
    apprent_text = "\n".join(
        f"- {a.get('contenu','')}" for a in lt.get("apprentissages", [])[-10:]
    ) or "Aucun."
    erreurs_text = "\n".join(
        f"- {e.get('contenu','')}" for e in lt.get("erreurs", [])[-10:]
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
    lt        = load_long_term()

    journal_text = "\n".join(
        f"- [{e['date'][:10]}] {e.get('auteur','?')}: {e.get('contenu','')}"
        for e in week
    ) or "Aucune entrée cette semaine."
    decisions_text = "\n".join(
        f"- {d.get('contenu','')}" for d in lt.get("decisions", [])[-5:]
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


async def scheduler_loop():
    last_crash_check      = None
    last_business_refresh = None
    last_autonomous_tick  = None
    last_pipeline_check   = None
    last_checkin_date     = None
    last_summary_week     = None
    last_brief_date       = None
    last_prof_email_week  = None

    while True:
        now    = now_utc()
        paris  = now_paris()
        is_weekend = paris.weekday() >= 5  # samedi=5, dimanche=6

        # Crash check toutes les heures
        if last_crash_check is None or (now - last_crash_check).total_seconds() >= 3600:
            await check_crashes()
            last_crash_check = now

        # Refresh business.json toutes les heures
        if last_business_refresh is None or (now - last_business_refresh).total_seconds() >= 3600:
            try:
                refresh_business()
            except Exception:
                pass
            last_business_refresh = now

        # Pipeline manager toutes les heures (indépendant du tick autonome)
        if last_pipeline_check is None or (now - last_pipeline_check).total_seconds() >= 3600:
            print(f"[PIPELINE] Vérification étape suivante... ({now_paris().strftime('%d/%m %H:%M')})")
            try:
                from autonomous_loop import pipeline_advance as _pipeline_advance
                acted = await _pipeline_advance(running_procs)
                if acted:
                    print("[PIPELINE] Action prise — étape suivante lancée.")
                else:
                    print("[PIPELINE] Rien à avancer pour l'instant.")
            except Exception as e:
                print(f"[PIPELINE] Erreur pipeline_advance : {e}")
            last_pipeline_check = now

        # Boucle autonome 2x par jour (pause le week-end)
        if not is_weekend and (last_autonomous_tick is None or (now - last_autonomous_tick).total_seconds() >= 43200):
            try:
                await autonomous_loop_tick(running_procs)
            except Exception:
                pass
            last_autonomous_tick = now

        today = paris.date()

        # Brief matin à 8h (jours ouvrés uniquement)
        if paris.hour == 8 and not is_weekend and last_brief_date != today:
            await morning_brief()
            last_brief_date = today

        # Check-in + rapport autonome à 21h (jours ouvrés uniquement)
        if paris.hour == 21 and not is_weekend and last_checkin_date != today:
            await daily_checkin()
            try:
                await daily_autonomous_report()
            except Exception:
                pass
            last_checkin_date = today

        # Consolidation différée du journal après check-in
        # - 30 min après dernière réponse si tout le monde a répondu
        # - sinon 2h après envoi (avec ce qu'on a, ou rien)
        # - pas de consolidation le week-end (le check-in n'est pas envoyé)
        if (
            checkin_state
            and not checkin_state.get("consolidated")
            and not is_weekend
        ):
            try:
                sent_at = datetime.datetime.fromisoformat(checkin_state["sent_at"])
                age_sec = (now - sent_at).total_seconds()
                responses = checkin_state.get("responses", {})
                ready = False

                if responses:
                    last_received = max(
                        datetime.datetime.fromisoformat(r["received_at"])
                        for r in responses.values()
                    )
                    if (now - last_received).total_seconds() >= CHECKIN_DELAY_AFTER_LAST:
                        ready = True

                if not ready and age_sec >= CHECKIN_HARD_DEADLINE:
                    ready = True  # 2h max — on consolide ce qu'on a (peut être vide)

                if ready:
                    await consolidate_checkin()
                    # Nettoyer les awaiting_journal restants pour éviter
                    # qu'une réponse tardive crée une deuxième entrée
                    awaiting_journal.clear()
            except Exception as e:
                print(f"[checkin] Erreur consolidation : {e}")

        # Résumé hebdo dimanche à 20h
        week_num = paris.isocalendar()[1]
        if paris.weekday() == 6 and paris.hour == 20 and last_summary_week != week_num:
            await weekly_summary()
            last_summary_week = week_num

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
        proc = subprocess.Popen(
            ["/opt/geo-leaad-fr-landscaping/.venv/bin/python",
             "/opt/geo-leaad-fr-landscaping/scripts/scheduler.py"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
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
        proc = subprocess.Popen(
            ["/opt/geo-leaad-fr-landscaping/.venv/bin/python",
             "/opt/geo-leaad-fr-landscaping/scripts/enrich.py"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        running_procs["enrich"] = proc
        _record_stage_started("enrich")
        await safe_reply(update, f"✅ Enrichissement lancé (PID {proc.pid})", markdown=False)
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
            kb_after = len(load_kb() or [])
            added    = kb_after - kb_before
            status   = f"✅ +{added} chunks" if added > 0 else "✅ Rien de nouveau"
            await send(chat_id, f"{status} — {kb_after} chunks au total\n```\n{out}\n```")
        except Exception as e:
            await send(chat_id, f"❌ Erreur ingestion : {e}")

    asyncio.create_task(_run())


async def cmd_brief(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update): return
    # Pas de broadcast : on répond uniquement au demandeur, sans déclencher
    # les alertes proactives (qui seraient broadcastées à tout le monde).
    recipient = founder_name(update.effective_chat.id)
    msg = await _generate_brief_text(trigger_alerts=False, recipient=recipient)
    await safe_reply(update, msg)


# ─── INGESTION DOCUMENT TELEGRAM ──────────────────────────────────────────────

SUPPORTED_MIME = {"application/pdf", "text/plain"}
SUPPORTED_EXT  = {".pdf", ".txt"}

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


async def cmd_memoire(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update): return
    lt       = load_long_term()
    business = load_business()
    kb       = load_kb()
    journal  = load_json(JOURNAL_FILE, [])

    parts = ["🧠 *Ce que je sais sur VAO*\n"]

    # Contexte
    ctx = lt.get("contexte_vao", {})
    if ctx:
        parts.append("*Contexte projet :*")
        for k, v in ctx.items():
            parts.append(f"  • {esc(str(k))} : {esc(str(v))}")

    # Décisions
    decisions = lt.get("decisions", [])
    if decisions:
        parts.append(f"\n*Décisions ({len(decisions)}) — 5 dernières :*")
        for d in decisions[-5:]:
            parts.append(f"  • [{d.get('date','')[:10]}] {esc(d.get('contenu',''))}")
    else:
        parts.append("\n*Décisions :* aucune mémorisée")

    # Apprentissages
    appren = lt.get("apprentissages", [])
    if appren:
        parts.append(f"\n*Apprentissages ({len(appren)}) — 5 derniers :*")
        for a in appren[-5:]:
            parts.append(f"  • [{a.get('date','')[:10]}] {esc(a.get('contenu',''))}")

    # Erreurs
    erreurs = lt.get("erreurs", [])
    if erreurs:
        parts.append(f"\n*Erreurs connues ({len(erreurs)}) :*")
        for e in erreurs[-3:]:
            parts.append(f"  • [{e.get('date','')[:10]}] {esc(e.get('contenu',''))}")

    # Business
    if business.get("derniere_maj"):
        parts.append(f"\n*Business* (mis à jour {business['derniere_maj'][:16].replace('T',' ')} UTC) :")
        parts.append(f"  • Leads total : {business.get('leads_total','?')}")
        parts.append(f"  • Enrichis : {business.get('enrichis','?')}")
        statuts = business.get("statuts", {})
        if statuts:
            parts.append("  • Statuts : " + ", ".join(f"{k}={v}" for k, v in statuts.items()))
    else:
        parts.append("\n*Business :* pas encore chargé (lancer /stats)")

    # Journal
    parts.append(f"\n*Journal :* {len(journal)} entrée(s) au total")

    # KB
    if kb:
        sources = list({c["source"] for c in kb})
        parts.append(f"*Knowledge base :* {len(kb)} chunks — {len(sources)} PDF(s)")
        if sources:
            parts.append("  Sources : " + ", ".join(sorted(sources)[:5]))
    else:
        parts.append("*Knowledge base :* vide (lance /ingest pour charger les PDFs)")

    await safe_reply(update, "\n".join(parts))


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
        "menu_autonome":      cmd_autonome,
        "menu_decisions":     cmd_decisions,
        "menu_plan":          cmd_plan,
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
        [B("🤖 Autonome",     callback_data="menu_autonome"),
         B("📜 Décisions",    callback_data="menu_decisions")],
        [B("🗓 Plan",         callback_data="menu_plan"),
         B("📌 Push actions", callback_data="menu_push")],

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
    if not authorized(update): return
    chat_id     = str(update.effective_chat.id)
    text        = update.message.text
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
        else:
            # Fallback : pas d'état actif → on écrit directement (cas réponse tardive)
            append_journal(chat_id, author_name, text, analyse)
        # Extraction mémoire en arrière-plan
        asyncio.create_task(extract_and_save_memory(text, analyse))
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
    stats   = get_vao_stats()
    journal = load_json(JOURNAL_FILE, [])[-5:]
    journal_text = "\n".join(
        f"- [{e['date'][:10]}] {e.get('auteur', e.get('author_name','?'))}: {e.get('contenu', e.get('content',''))}"
        for e in journal
    ) or "Aucune entrée récente."

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
    user_content = (
        f"Stats VAO : {stats_text}\n"
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
        sys_prompt = build_system_prompt()
        cur_messages = list(messages)
        for iteration in range(2):
            resp = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=4000,
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
    except Exception as e:
        if not reply:
            reply = f"Erreur Claude : {e}"
        else:
            reply += f"\n\n[continuation interrompue : {e}]"

    # Mise à jour de l'historique
    add_to_history(chat_id, "user", text)
    add_to_history(chat_id, "assistant", reply)

    # Extraction mémoire en arrière-plan (silencieuse)
    asyncio.create_task(extract_and_save_memory(text, reply))

    # Le mode autonome post-réponse a été retiré : il créait des faux positifs
    # à chaque message contenant des verbes d'action. Les tâches s'ajoutent
    # désormais uniquement via :
    #   - une demande explicite ("ajoute une tâche…")
    #   - la commande /push
    #   - le check-in du soir
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
    if not LONG_TERM_FILE.exists():
        save_json(LONG_TERM_FILE, default_long_term())
        print("[startup] long_term.json créé.")
    if not BUSINESS_FILE.exists():
        save_json(BUSINESS_FILE, default_business())
        print("[startup] business.json créé.")


# ─── MAIN ─────────────────────────────────────────────────────────────────────

async def main():
    init_memory_files()
    auto_ingest_if_empty()

    # Restauration de l'historique de conversation (persistant à travers les restarts)
    load_history_from_disk()

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
    app.add_handler(CommandHandler("aide",           cmd_aide))
    app.add_handler(CommandHandler("menu",           cmd_menu))
    # Boucle autonome
    app.add_handler(CommandHandler("autonome",       cmd_autonome))
    app.add_handler(CommandHandler("decisions",      cmd_decisions))
    app.add_handler(CommandHandler("plan",           cmd_plan))
    # Routing des boutons du menu interactif (pattern ^menu_).
    app.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^menu_"))
    app.add_handler(MessageHandler(filters.Document.PDF | filters.Document.TXT, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    # Message de démarrage avec bouton "📋 Menu" sous le texte.
    startup_text = (
        f"🤖 *{BOT_NAME} en ligne*\n"
        "Co-fondateur virtuel VAO opérationnel."
    )
    startup_markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("📋 Menu", callback_data="menu_aide")]]
    )
    for cid in CHAT_IDS:
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
