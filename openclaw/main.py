import os, json, asyncio, datetime, subprocess, shutil, requests, re
from pathlib import Path
from collections import deque
from dotenv import load_dotenv
import anthropic
from telegram import Bot, Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes
from autonomous_loop import (
    autonomous_loop_tick,
    daily_autonomous_report,
    cmd_autonome,
    cmd_decisions,
    cmd_plan,
    _record_stage_started,
)

load_dotenv("/opt/openclaw/.env")

BASE           = Path("/opt/openclaw")
JOURNAL_FILE   = BASE / "memory/journal.json"
ALERT_FILE     = BASE / "memory/alert_state.json"
KB_FILE        = BASE / "memory/knowledge_base.json"
LONG_TERM_FILE = BASE / "memory/long_term.json"
BUSINESS_FILE  = BASE / "memory/business.json"
COSTS_FILE     = BASE / "memory/costs.json"

EXTRACT_DAILY_LIMIT  = 20       # extractions max par jour
TOKEN_DAILY_LIMIT    = 100_000  # tokens/jour au-delà desquels on coupe extract

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
BOT_TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID_1    = os.getenv("TELEGRAM_CHAT_ID_1")
CHAT_ID_2    = os.getenv("TELEGRAM_CHAT_ID_2")
API_VAO_KEY  = os.getenv("API_VAO_KEY", "")
API_VAO_URL  = os.getenv("API_VAO_URL", "https://178-104-104-36.sslip.io")

client   = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
CHAT_IDS = [c for c in [CHAT_ID_1, CHAT_ID_2] if c]

awaiting_journal: set[str] = set()
running_procs: dict[str, subprocess.Popen] = {}

# Fenêtre glissante 20 messages par chat_id
conv_history: dict[str, deque] = {}
HISTORY_LIMIT = 20


# ─── HELPERS ──────────────────────────────────────────────────────────────────

def now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)

def now_paris() -> datetime.datetime:
    return now_utc().astimezone(datetime.timezone(datetime.timedelta(hours=2)))

def load_json(path: Path, default):
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return default

def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

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
        "cours_chunks": [],
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

def get_vao_stats() -> dict:
    try:
        r = requests.get(
            f"{API_VAO_URL}/stats",
            headers={"X-API-Key": API_VAO_KEY},
            timeout=10,
        )
        return r.json() if r.status_code == 200 else {"error": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"error": str(e)}

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
    words = set(re.findall(r'\b\w{3,}\b', query.lower()))
    if not words:
        return []
    scores: dict[int, int] = {}
    for i, chunk in enumerate(kb):
        chunk_words = set(re.findall(r'\b\w{3,}\b', chunk["content"].lower()))
        score = len(words & chunk_words)
        if score > 0:
            scores[i] = score
    top_indices = sorted(scores, key=lambda x: scores[x], reverse=True)[:top_k]
    return [kb[i]["content"] for i in top_indices]


# ─── SYSTEM PROMPT ENRICHI ────────────────────────────────────────────────────

def build_system_prompt() -> str:
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

    return "\n".join(parts)


# ─── HISTORIQUE DE CONVERSATION ───────────────────────────────────────────────

def get_history(chat_id: str) -> list[dict]:
    if chat_id not in conv_history:
        conv_history[chat_id] = deque(maxlen=HISTORY_LIMIT)
    return list(conv_history[chat_id])

def add_to_history(chat_id: str, role: str, content: str):
    if chat_id not in conv_history:
        conv_history[chat_id] = deque(maxlen=HISTORY_LIMIT)
    conv_history[chat_id].append({"role": role, "content": content})


# ─── EXTRACTION MÉMOIRE ───────────────────────────────────────────────────────

async def extract_and_save_memory(user_text: str, ai_response: str):
    """
    Appel Haiku léger pour extraire décisions/apprentissages/erreurs.
    Garde-fous : message > 50 mots, max 20 extractions/jour, max 100k tokens/jour.
    """
    # Garde-fou 1 : message trop court → pas d'extraction
    if len(user_text.split()) < 50:
        return

    # Garde-fou 2 : limites journalières atteintes
    if not can_extract():
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
    except Exception:
        pass  # extraction silencieuse — ne bloque jamais la conversation


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
            model="claude-opus-4-5",
            max_tokens=200,
            system=build_system_prompt(),
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip()
    except Exception as e:
        return f"(Analyse impossible : {e})"


# ─── TELEGRAM ─────────────────────────────────────────────────────────────────

async def send(chat_id: str, text: str):
    bot = Bot(token=BOT_TOKEN)
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
    except Exception:
        clean = re.sub(r'[*_`\[\]]', '', text)
        try:
            await bot.send_message(chat_id=chat_id, text=clean)
        except Exception:
            pass

async def broadcast(text: str):
    for cid in CHAT_IDS:
        await send(cid, text)

def authorized(update: Update) -> bool:
    return str(update.effective_chat.id) in CHAT_IDS

def esc(s: str) -> str:
    return re.sub(r'[*_`\[\]]', '', str(s))

async def safe_reply(update: Update, text: str, markdown: bool = True):
    if markdown:
        try:
            await update.message.reply_text(text, parse_mode="Markdown")
            return
        except Exception:
            pass
    clean = re.sub(r'[*_`\[\]\\]', '', text)
    await update.message.reply_text(clean)


def tail_logs(n: int = 20) -> str:
    log = Path("/opt/geo-leaad-fr-landscaping/logs/app.log")
    if not log.exists():
        return "Fichier de log introuvable."
    try:
        r = subprocess.run(["tail", "-n", str(n), str(log)], capture_output=True, text=True)
        return r.stdout or "Log vide."
    except Exception as e:
        return f"Erreur : {e}"


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
        await broadcast("⚠️ *Alerte CroustyLobster*\n\n" + "\n".join(problems))


async def check_proactive_alerts(stats: dict):
    """Analyse autonome : contactés, scraper inactif, trajectoire objectif."""
    alert = load_json(ALERT_FILE, {})
    now   = now_utc()
    paris = now_paris()
    problems = []

    def should_alert(key: str, hours: int = 24) -> bool:
        last = alert.get(key)
        if not last:
            return True
        return (now - datetime.datetime.fromisoformat(last)).total_seconds() > hours * 3600

    # Aucun lead contacté depuis 7 jours
    statuts = stats.get("repartition_statut", {})
    contactes = statuts.get("contacte", 0)
    offres    = statuts.get("offre_envoyee", 0)
    leads_total = stats.get("total_leads", stats.get("leads", 0))

    # On vérifie dans le journal si des contacts ont été mentionnés cette semaine
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
        problems.append(
            f"📭 *Aucun lead contacté cette semaine.*\n"
            f"Tu as {leads_total} leads en base. Prochaine action ?"
        )
        alert["no_contact_7d"] = now.isoformat()
    else:
        alert.pop("no_contact_7d", None)

    # Scraper inactif depuis 48h
    scraper_proc = running_procs.get("scraper")
    scraper_running = scraper_proc and scraper_proc.poll() is None
    last_scraper = alert.get("last_scraper_active")
    if last_scraper:
        last_scraper_dt = datetime.datetime.fromisoformat(last_scraper)
        scraper_silent_h = (now - last_scraper_dt).total_seconds() / 3600
    else:
        scraper_silent_h = 0

    if scraper_running:
        alert["last_scraper_active"] = now.isoformat()
        alert.pop("scraper_inactive_48h", None)
    else:
        # Ne pas alerter si le scraper a terminé normalement (exit code 0)
        scraper_exit_code = alert.get("last_scraper_exit_code")
        scraper_finished  = alert.get("last_scraper_finished")
        scraper_ended_ok  = (scraper_finished is not None and scraper_exit_code == 0)

        if not scraper_ended_ok and scraper_silent_h > 48 and last_scraper and should_alert("scraper_inactive_48h", hours=24):
            problems.append(
                f"🕷️ *Scraper inactif depuis {int(scraper_silent_h)}h.*\n"
                f"Lance-le avec /start_scraper si nécessaire."
            )
            alert["scraper_inactive_48h"] = now.isoformat()
        elif scraper_ended_ok:
            alert.pop("scraper_inactive_48h", None)

    # Trajectoire hebdo : si peu d'entrées journal en milieu de semaine
    weekday = paris.weekday()  # 0=lundi, 6=dimanche
    if weekday in (2, 3) and len(recent_journal) < 2 and should_alert("low_activity", hours=48):
        problems.append(
            f"📉 *Peu d'activité journalisée cette semaine* ({len(recent_journal)} entrée(s)).\n"
            f"Objectifs hebdo sur la bonne trajectoire ?"
        )
        alert["low_activity"] = now.isoformat()

    save_json(ALERT_FILE, alert)
    if problems:
        await broadcast("🔔 *Analyse proactive VAO*\n\n" + "\n\n".join(problems))


async def morning_brief():
    paris     = now_paris()
    today_str = paris.strftime("%d/%m/%Y")

    # Agents actifs
    agent_lines = []
    for name, p in running_procs.items():
        st = "🟢 actif" if p.poll() is None else "🔴 arrêté"
        agent_lines.append(f"  • {name} : {st}")
    if not agent_lines:
        agent_lines.append("  • Aucun agent actif")

    # Stats VAO + refresh business
    stats = get_vao_stats()
    refresh_business()

    if "error" not in stats:
        leads     = stats.get("total_leads", stats.get("leads", "?"))
        nouveaux  = stats.get("repartition_statut", {}).get("nouveau", "?")
        contactes = stats.get("repartition_statut", {}).get("contacte", "?")
        offres    = stats.get("repartition_statut", {}).get("offre_envoyee", "?")
        stats_line = f"Leads : {leads} | Nouveaux : {nouveaux} | Contactés : {contactes} | Offres : {offres}"
    else:
        stats_line = f"API indisponible ({stats['error']})"

    # Rappels Supabase
    rappels_lines: list[str] = []
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
                    rappels_lines.append(f"  • {label}")
            elif r.status_code == 400:
                rappels_lines.append("  • Table campaign_leads : schéma à configurer")
            else:
                rappels_lines.append(f"  • Erreur Supabase HTTP {r.status_code}")
        except Exception as e:
            rappels_lines.append(f"  • Erreur Supabase : {e}")
    else:
        rappels_lines.append("  • Supabase non configuré")

    rappels_text = "\n".join(rappels_lines) or "  • Aucun rappel"

    # Objectif du jour via Claude
    journal      = load_json(JOURNAL_FILE, [])
    cutoff       = now_utc() - datetime.timedelta(days=7)
    recent       = [e for e in journal if datetime.datetime.fromisoformat(e["date"]) > cutoff]
    journal_text = "\n".join(
        f"- [{e['date'][:10]}] {e.get('auteur', e.get('author_name','?'))}: {e.get('contenu', e.get('content',''))}"
        for e in recent
    ) or "Aucune entrée cette semaine."

    try:
        resp = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=200,
            messages=[{"role": "user", "content": (
                f"Journal VAO de la semaine :\n{journal_text}\n\n"
                f"Stats : {stats_line}\n\n"
                "En une phrase courte et motivante, génère l'objectif prioritaire du jour "
                "pour le projet VAO. Direct, actionnable, en français."
            )}],
        )
        objectif = resp.content[0].text.strip()
    except Exception as e:
        objectif = f"(Erreur Claude : {e})"

    # Alertes proactives en même temps que le brief
    if "error" not in stats:
        await check_proactive_alerts(stats)

    msg = (
        f"🌅 *Bonjour Quentin ! Brief VAO du {today_str}*\n\n"
        f"*Agents :*\n" + "\n".join(agent_lines) + "\n\n"
        f"📊 {stats_line}\n\n"
        f"*Rappels du jour :*\n{rappels_text}\n\n"
        f"🎯 *Objectif du jour :* {objectif}"
    )
    await broadcast(msg)


async def daily_checkin():
    for cid in CHAT_IDS:
        awaiting_journal.add(cid)
        await send(cid, "Bonsoir Quentin ! Qu'est-ce que t'as fait aujourd'hui sur VAO ?")


async def weekly_summary():
    journal  = load_json(JOURNAL_FILE, [])
    cutoff   = now_utc() - datetime.timedelta(days=7)
    week     = [e for e in journal if datetime.datetime.fromisoformat(e["date"]) > cutoff]
    lt       = load_long_term()
    business = load_business()

    if not week:
        await broadcast("📋 *Résumé hebdo VAO*\n\nAucune entrée de journal cette semaine.")
        return

    journal_text = "\n".join(
        f"- [{e['date'][:10]}] {e.get('auteur', e.get('author_name','?'))}: {e.get('contenu', e.get('content',''))}"
        for e in week
    )
    decisions_text = "\n".join(
        f"- {d.get('contenu','')}" for d in lt.get("decisions", [])[-10:]
    ) or "Aucune."

    prompt = (
        f"Journal de la semaine du projet VAO :\n{journal_text}\n\n"
        f"Décisions récentes en mémoire :\n{decisions_text}\n\n"
        f"Stats actuelles : leads={business.get('leads_total')}, "
        f"statuts={json.dumps(business.get('statuts',{}))}\n\n"
        "Génère un résumé structuré :\n"
        "1. **Ce qui a été fait** (bullet points)\n"
        "2. **Points bloquants identifiés**\n"
        "3. **Comparaison objectifs vs réalisé**\n"
        "4. **3 priorités pour la semaine prochaine**\n\n"
        "Direct, concis, en français."
    )
    try:
        resp    = client.messages.create(
            model="claude-opus-4-5", max_tokens=1200,
            messages=[{"role": "user", "content": prompt}]
        )
        summary = resp.content[0].text
    except Exception as e:
        summary = f"Erreur génération : {e}"

    await broadcast(f"📋 *Résumé hebdo VAO*\n\n{summary}")


async def scheduler_loop():
    last_crash_check      = None
    last_business_refresh = None
    last_autonomous_tick  = None
    last_checkin_date     = None
    last_summary_week     = None
    last_brief_date       = None
    last_report_date      = None

    while True:
        now   = now_utc()
        paris = now_paris()

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

        # Boucle autonome toutes les heures
        if last_autonomous_tick is None or (now - last_autonomous_tick).total_seconds() >= 3600:
            try:
                await autonomous_loop_tick(running_procs)
            except Exception:
                pass
            last_autonomous_tick = now

        today = paris.date()

        # Brief matin à 8h
        if paris.hour == 8 and last_brief_date != today:
            await morning_brief()
            last_brief_date = today

        # Check-in + rapport autonome à 21h
        if paris.hour == 21 and last_checkin_date != today:
            await daily_checkin()
            try:
                await daily_autonomous_report()
            except Exception:
                pass
            last_checkin_date = today

        # Résumé hebdo dimanche à 20h
        week_num = paris.isocalendar()[1]
        if paris.weekday() == 6 and paris.hour == 20 and last_summary_week != week_num:
            await weekly_summary()
            last_summary_week = week_num

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
    await safe_reply(update, f"```\n{logs[-3800:]}\n```")


async def cmd_ingest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update): return
    await safe_reply(update, "📚 Ingestion des PDFs en cours...", markdown=False)
    try:
        result = subprocess.run(
            ["/opt/openclaw/venv/bin/python", "/opt/openclaw/scripts/ingest_docs.py"],
            capture_output=True, text=True, timeout=120,
        )
        out = result.stdout.strip() or result.stderr.strip() or "Terminé sans sortie."
        await safe_reply(update, f"```\n{out[-3000:]}\n```")
    except subprocess.TimeoutExpired:
        await safe_reply(update, "❌ Timeout — ingestion trop longue.", markdown=False)
    except Exception as e:
        await safe_reply(update, f"❌ Erreur : {e}", markdown=False)


async def cmd_brief(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update): return
    await morning_brief()


# ─── INGESTION DOCUMENT TELEGRAM ──────────────────────────────────────────────

def _extract_text_pdf(path: Path) -> str:
    import pdfplumber
    parts = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            t = page.extract_text()
            if t:
                parts.append(t)
    return "\n".join(parts)

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

def _ingest_pdf(pdf_path: Path) -> tuple[int, bool]:
    """
    Ingère un PDF dans la KB. Retourne (nb_chunks_ajoutés, déjà_connu).
    """
    source = pdf_path.stem
    kb = load_kb() or []
    existing_sources = {c["source"] for c in kb}

    if source in existing_sources:
        return 0, True

    text = _extract_text_pdf(pdf_path)
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

    doc = update.message.document
    if not doc.mime_type or doc.mime_type != "application/pdf":
        await safe_reply(update, "⚠️ Seuls les PDFs sont supportés pour l'instant.", markdown=False)
        return

    # Nom de fichier sécurisé
    raw_name = doc.file_name or f"doc_{doc.file_id}.pdf"
    safe_name = re.sub(r'[^\w\s\-.]', '_', raw_name).strip()
    if not safe_name.lower().endswith(".pdf"):
        safe_name += ".pdf"

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
        added, already_known = _ingest_pdf(dest)
    except Exception as e:
        await safe_reply(update, f"❌ Erreur ingestion : {e}", markdown=False)
        return

    if already_known:
        await safe_reply(update, f"ℹ️ *{esc(raw_name)}* est déjà dans la knowledge base.")
        return

    if added == 0:
        await safe_reply(update, f"⚠️ PDF reçu mais aucun texte extrait (*{esc(raw_name)}*).", markdown=False)
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


async def cmd_aide(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update): return
    msg = (
        "🦞 *CroustyLobster — Commandes VAO*\n\n"
        "/stats — Stats API (leads, statuts)\n"
        "/start_scraper — Lance le scraper géo\n"
        "/stop_scraper — Arrête le scraper\n"
        "/start_enrich — Lance l'enrichissement\n"
        "/stop_enrich — Arrête l'enrichissement\n"
        "/status — État agents + disque/RAM\n"
        "/logs — Dernières 20 lignes de logs\n"
        "/ingest — Ingère les PDFs de /opt/openclaw/docs/\n"
        "/brief — Force le brief matin\n"
        "/memoire — Résumé de ce que je sais sur le projet\n"
        "/autonome — Active/désactive la boucle autonome\n"
        "/decisions — 10 dernières décisions prises seul\n"
        "/plan — Ce que l'agent prévoit de faire demain\n"
        "/aide — Cette aide\n\n"
        "Envoie n'importe quel message pour me poser une question."
    )
    await safe_reply(update, msg)


# ─── MESSAGES LIBRES ──────────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update): return
    chat_id     = str(update.effective_chat.id)
    text        = update.message.text
    author_name = update.effective_user.first_name or "Fondateur"

    # Réponse au check-in quotidien
    if chat_id in awaiting_journal:
        awaiting_journal.discard(chat_id)
        analyse = await analyze_checkin(author_name, text)
        append_journal(chat_id, author_name, text, analyse)
        # Extraction mémoire en arrière-plan
        asyncio.create_task(extract_and_save_memory(text, analyse))
        reply = f"✅ *Noté dans le journal !*\n\n{analyse}"
        await safe_reply(update, reply)
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
        f"Utilisateur : {author_name}\n\n"
        f"{text}"
    )

    # Si historique existe, on injecte le contexte seulement dans le nouveau message
    messages = history + [{"role": "user", "content": user_content}]

    try:
        resp  = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=800,
            system=build_system_prompt(),
            messages=messages,
        )
        reply = resp.content[0].text
    except Exception as e:
        reply = f"Erreur Claude : {e}"

    # Mise à jour de l'historique
    add_to_history(chat_id, "user", text)
    add_to_history(chat_id, "assistant", reply)

    # Extraction mémoire en arrière-plan (silencieuse)
    asyncio.create_task(extract_and_save_memory(text, reply))

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
    app.add_handler(CommandHandler("aide",           cmd_aide))
    # Boucle autonome
    app.add_handler(CommandHandler("autonome",       cmd_autonome))
    app.add_handler(CommandHandler("decisions",      cmd_decisions))
    app.add_handler(CommandHandler("plan",           cmd_plan))
    app.add_handler(MessageHandler(filters.Document.PDF, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    await broadcast(
        "🦞 *CroustyLobster en ligne*\n"
        "Co-fondateur virtuel VAO opérationnel.\n"
        "/aide pour la liste des commandes."
    )

    await scheduler_loop()


if __name__ == "__main__":
    asyncio.run(main())
