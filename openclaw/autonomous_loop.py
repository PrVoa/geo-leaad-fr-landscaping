"""
autonomous_loop.py — Boucle autonome CroustyLobster
Analyse la situation VAO toutes les heures et prend des décisions seul.
Intégré dans scheduler_loop() de main.py.
"""

import os, json, datetime, subprocess, re
from pathlib import Path
from dotenv import load_dotenv
import anthropic
from telegram import Bot

load_dotenv("/opt/openclaw/.env")

BASE                 = Path("/opt/openclaw")
AUTONOMOUS_LOG_FILE  = BASE / "memory/autonomous_log.json"
AUTONOMOUS_MODE_FILE = BASE / "memory/autonomous_mode.json"
JOURNAL_FILE         = BASE / "memory/journal.json"
LONG_TERM_FILE       = BASE / "memory/long_term.json"
BUSINESS_FILE        = BASE / "memory/business.json"
ALERT_FILE           = BASE / "memory/alert_state.json"

BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN")
_CHAT_IDS  = [c for c in [os.getenv("TELEGRAM_CHAT_ID_1"), os.getenv("TELEGRAM_CHAT_ID_2")] if c]
API_VAO_URL = os.getenv("API_VAO_URL", "https://178-104-104-36.sslip.io")
API_VAO_KEY = os.getenv("API_VAO_KEY", "")

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

SCRAPER_CMD = [
    "/opt/geo-leaad-fr-landscaping/.venv/bin/python",
    "/opt/geo-leaad-fr-landscaping/scripts/scheduler.py",
]
ENRICH_CMD = [
    "/opt/geo-leaad-fr-landscaping/.venv/bin/python",
    "/opt/geo-leaad-fr-landscaping/scripts/enrich.py",
]
CLEAN_CMD = [
    "/opt/geo-leaad-fr-landscaping/.venv/bin/python",
    "/opt/geo-leaad-fr-landscaping/scripts/clean.py",
]

MAX_ACTIONS_PER_DAY = 3


# ─── HELPERS ──────────────────────────────────────────────────────────────────

def _load_json(path: Path, default):
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return default

def _save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def _now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)

def _now_paris() -> datetime.datetime:
    return _now_utc().astimezone(datetime.timezone(datetime.timedelta(hours=2)))

def _esc(s: str) -> str:
    for ch in ["_", "*", "`", "["]:
        s = str(s).replace(ch, f"\\{ch}")
    return s


# ─── MODE AUTONOME ────────────────────────────────────────────────────────────

def is_autonomous_mode() -> bool:
    """Lit l'état depuis le fichier (toggle /autonome), fallback sur .env."""
    state = _load_json(AUTONOMOUS_MODE_FILE, None)
    if state is not None:
        return state.get("active", False)
    return os.getenv("AUTONOMOUS_MODE", "false").lower() == "true"

def set_autonomous_mode(active: bool):
    _save_json(AUTONOMOUS_MODE_FILE, {
        "active": active,
        "changed_at": _now_utc().isoformat(),
    })


# ─── LOG AUTONOME ─────────────────────────────────────────────────────────────

def load_autonomous_log() -> list:
    return _load_json(AUTONOMOUS_LOG_FILE, [])

def append_autonomous_log(entry: dict):
    log = load_autonomous_log()
    log.append(entry)
    if len(log) > 200:
        log = log[-200:]
    _save_json(AUTONOMOUS_LOG_FILE, log)

def count_actions_today() -> int:
    """Compte les actions réelles du jour (exclut ATTENDRE et ALERTER)."""
    log = load_autonomous_log()
    today = _now_utc().date().isoformat()
    return sum(
        1 for e in log
        if e.get("date", "")[:10] == today
        and e.get("action") not in ("ATTENDRE", "ALERTER")
        and not e.get("result", "").startswith("ANNULÉE")
    )


# ─── TELEGRAM ─────────────────────────────────────────────────────────────────

async def _send(chat_id: str, text: str):
    bot = Bot(token=BOT_TOKEN)
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
    except Exception:
        clean = re.sub(r'[*_`\[\]]', '', text)
        try:
            await bot.send_message(chat_id=chat_id, text=clean)
        except Exception:
            pass

async def _broadcast(text: str):
    for cid in _CHAT_IDS:
        await _send(cid, text)

async def _reply(update, text: str):
    try:
        await update.message.reply_text(text, parse_mode="Markdown")
    except Exception:
        clean = re.sub(r'[*_`\[\]\\]', '', text)
        try:
            await update.message.reply_text(clean)
        except Exception:
            pass

def _authorized(update) -> bool:
    return str(update.effective_chat.id) in _CHAT_IDS


# ─── CONTEXTE ─────────────────────────────────────────────────────────────────

def _build_context(any_agent_running: bool = False) -> str:
    """Construit un résumé de situation pour Claude."""
    import requests as _req

    # Stats VAO
    try:
        r = _req.get(f"{API_VAO_URL}/stats", headers={"X-API-Key": API_VAO_KEY}, timeout=10)
        stats = r.json() if r.status_code == 200 else {"error": f"HTTP {r.status_code}"}
    except Exception as e:
        stats = {"error": str(e)}

    # Business cache
    business = _load_json(BUSINESS_FILE, {})

    # Journal 7 derniers jours
    journal = _load_json(JOURNAL_FILE, [])
    cutoff  = _now_utc() - datetime.timedelta(days=7)
    recent_journal = [
        e for e in journal
        if datetime.datetime.fromisoformat(e["date"]) > cutoff
    ]

    # Long term memory
    lt = _load_json(LONG_TERM_FILE, {})
    decisions = lt.get("decisions", [])[-5:]

    # Historique actions autonomes
    auto_log = load_autonomous_log()

    # État scraper depuis alert_state
    alert = _load_json(ALERT_FILE, {})
    last_scraper = alert.get("last_scraper_active", "jamais")
    if last_scraper != "jamais":
        try:
            dt = datetime.datetime.fromisoformat(last_scraper)
            h_ago = round((_now_utc() - dt).total_seconds() / 3600, 1)
            last_scraper = f"il y a {h_ago}h"
        except Exception:
            pass

    parts = [
        f"=== STATS VAO (API) ===\n{json.dumps(stats, ensure_ascii=False, indent=2)}",
        f"\n=== BUSINESS (cache) ===",
        f"Leads total : {business.get('leads_total', '?')}",
        f"Enrichis    : {business.get('enrichis', '?')}",
        f"Nettoyés    : {business.get('nettoyes', '?')}",
        f"Statuts     : {json.dumps(business.get('statuts', {}), ensure_ascii=False)}",
        f"\n=== JOURNAL 7 JOURS ({len(recent_journal)} entrées) ===",
    ]
    for e in recent_journal[-5:]:
        parts.append(f"[{e.get('date','')[:10]}] {e.get('auteur','?')}: {e.get('contenu','')[:200]}")

    parts.append("\n=== DÉCISIONS RÉCENTES ===")
    for d in decisions:
        parts.append(f"[{d.get('date','')[:10]}] {d.get('contenu','')}")

    parts.append("\n=== 5 DERNIÈRES ACTIONS AUTONOMES ===")
    for a in auto_log[-5:]:
        parts.append(
            f"[{a.get('date','')[:10]}] {a.get('action','')} "
            f"(conf={a.get('confidence','?')}) — {a.get('reason','')[:80]} → {a.get('result','')[:40]}"
        )

    parts.append(f"\n=== ÉTAT INFRA ===")
    parts.append(f"Dernier scraper actif : {last_scraper}")
    parts.append(f"Agent en cours        : {'OUI' if any_agent_running else 'non'}")
    parts.append(f"Actions prises aujourd'hui : {count_actions_today()}/{MAX_ACTIONS_PER_DAY}")

    return "\n".join(parts)


# ─── ANALYSE ET DÉCISION ──────────────────────────────────────────────────────

async def analyze_and_decide(any_agent_running: bool = False) -> dict:
    """
    Appelle Claude pour analyser la situation et décider d'une action.
    Retourne {"action": str, "reason": str, "confidence": str, "message_alert": str|None}
    """
    context = _build_context(any_agent_running)
    actions_remaining = MAX_ACTIONS_PER_DAY - count_actions_today()

    prompt = f"""Tu es CroustyLobster, IA co-fondatrice de VAO (SaaS devis paysagistes).
Tu tournes en mode autonome et dois décider d'une action proactive.

Contexte :
{context}

Tu dois choisir UNE action :
- SCRAPER   : lancer le scraper géo (si peu de nouveaux leads récents ou scraper inactif >48h)
- ENRICHIR  : lancer l'enrichissement (si des leads n'ont pas de gérant identifié)
- NETTOYER  : lancer le nettoyage (si des leads hors-cible sont détectés)
- ALERTER   : prévenir les fondateurs si situation critique ou hésitation
- ATTENDRE  : rien à faire, situation normale

RÈGLES IMPÉRATIVES :
1. Si confiance < 80% et action active (SCRAPER/ENRICHIR/NETTOYER) → choisir ALERTER
2. Si un agent est déjà en cours (any_agent_running=OUI) → ATTENDRE ou ALERTER uniquement
3. Si actions_remaining = 0 → ATTENDRE ou ALERTER uniquement
4. En cas de doute → ALERTER, jamais agir à l'aveugle

Réponds uniquement en JSON strict :
{{"action": "ACTION", "reason": "explication en français (1-2 phrases)", "confidence": "high|medium|low", "message_alert": "texte du message si action=ALERTER, sinon null"}}"""

    try:
        resp = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if m:
            result = json.loads(m.group())
            # Normalise l'action
            valid = {"SCRAPER", "ENRICHIR", "NETTOYER", "ALERTER", "ATTENDRE"}
            if result.get("action") not in valid:
                result["action"] = "ATTENDRE"
            return result
    except Exception as e:
        print(f"[autonomous] Erreur analyze_and_decide : {e}")

    return {"action": "ATTENDRE", "reason": "Erreur interne, attente par précaution", "confidence": "low", "message_alert": None}


# ─── EXÉCUTION ────────────────────────────────────────────────────────────────

async def execute_action(decision: dict, running_procs: dict) -> str:
    """Exécute l'action choisie. Retourne une description du résultat."""
    action     = decision.get("action", "ATTENDRE")
    reason     = decision.get("reason", "")
    confidence = decision.get("confidence", "low")

    # Garde-fou confiance faible
    if confidence == "low" and action not in ("ATTENDRE", "ALERTER"):
        decision["message_alert"] = (
            f"⚠️ J'hésite sur l'action à prendre.\n"
            f"J'envisageais : *{action}*\n"
            f"Raison : {reason}\n\n"
            f"Confiance faible — je vous laisse décider."
        )
        decision["action"] = "ALERTER"
        action = "ALERTER"

    # Garde-fou double agent
    any_running = any(p.poll() is None for p in running_procs.values())
    if any_running and action in ("SCRAPER", "ENRICHIR", "NETTOYER"):
        print(f"[autonomous] Action {action} annulée — agent déjà actif")
        return "ANNULÉE — agent déjà actif"

    # Garde-fou quota journalier
    if count_actions_today() >= MAX_ACTIONS_PER_DAY and action not in ("ATTENDRE", "ALERTER"):
        print(f"[autonomous] Action {action} annulée — quota journalier atteint")
        return "ANNULÉE — quota journalier atteint"

    if action == "SCRAPER":
        try:
            proc = subprocess.Popen(SCRAPER_CMD, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            running_procs["scraper"] = proc
            alert = _load_json(ALERT_FILE, {})
            alert["last_scraper_active"] = _now_utc().isoformat()
            _save_json(ALERT_FILE, alert)
            result = f"Scraper lancé (PID {proc.pid})"
            await _broadcast(
                f"🤖 *Action autonome — SCRAPER*\n"
                f"_{reason}_\n\n✅ {result}"
            )
            return result
        except Exception as e:
            return f"Erreur lancement scraper : {e}"

    elif action == "ENRICHIR":
        try:
            proc = subprocess.Popen(ENRICH_CMD, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            running_procs["enrich"] = proc
            result = f"Enrichissement lancé (PID {proc.pid})"
            await _broadcast(
                f"🤖 *Action autonome — ENRICHIR*\n"
                f"_{reason}_\n\n✅ {result}"
            )
            return result
        except Exception as e:
            return f"Erreur lancement enrichissement : {e}"

    elif action == "NETTOYER":
        try:
            clean_script = Path(CLEAN_CMD[-1])
            if not clean_script.exists():
                msg = f"⚠️ Script de nettoyage introuvable ({clean_script}). Action annulée."
                await _broadcast(f"🤖 *Action autonome — NETTOYER annulée*\n{msg}")
                return "ANNULÉE — script clean.py introuvable"
            proc = subprocess.Popen(CLEAN_CMD, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            running_procs["clean"] = proc
            result = f"Nettoyage lancé (PID {proc.pid})"
            await _broadcast(
                f"🤖 *Action autonome — NETTOYER*\n"
                f"_{reason}_\n\n✅ {result}"
            )
            return result
        except Exception as e:
            return f"Erreur lancement nettoyage : {e}"

    elif action == "ALERTER":
        msg = decision.get("message_alert") or reason
        await _broadcast(f"🔔 *Alerte autonome*\n\n{msg}")
        return "Alerte envoyée"

    else:  # ATTENDRE
        print(f"[autonomous] ATTENDRE — {reason}")
        return "Rien à faire"


# ─── RAPPORT QUOTIDIEN (21h) ──────────────────────────────────────────────────

async def daily_autonomous_report():
    """Rapport quotidien des actions prises de façon autonome."""
    if not is_autonomous_mode():
        return

    log   = load_autonomous_log()
    today = _now_paris().date().isoformat()
    today_entries = [e for e in log if e.get("date", "")[:10] == today]

    if not today_entries:
        return  # Pas d'actions aujourd'hui — rapport silencieux

    actions_text = "\n".join(
        f"- {e.get('action','')} : {e.get('reason','')[:100]} → {e.get('result','')[:60]}"
        for e in today_entries
    )
    context = _build_context()

    prompt = f"""Tu es CroustyLobster. Voici tes actions autonomes d'aujourd'hui :

{actions_text}

Contexte actuel :
{context}

Génère un rapport quotidien (5-8 lignes) au format EXACT :
"Aujourd'hui j'ai :
- [liste des actions prises et observations]

Demain je prévois de : [action planifiée et pourquoi, en 1-2 phrases]"

Direct, factuel, en français, tutoie les fondateurs."""

    try:
        resp = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        report = resp.content[0].text.strip()
    except Exception:
        report = "Rapport automatique :\n" + actions_text

    await _broadcast(f"📊 *Rapport autonome — {today}*\n\n{report}")


# ─── TICK HORAIRE ─────────────────────────────────────────────────────────────

async def autonomous_loop_tick(running_procs: dict):
    """
    À appeler toutes les heures depuis scheduler_loop() de main.py.
    Pipeline complet : analyse → décision → exécution → log → apprentissage.
    """
    if not is_autonomous_mode():
        return

    print(f"[autonomous] Tick — {_now_paris().strftime('%d/%m %H:%M')} (Paris)")

    any_running = any(p.poll() is None for p in running_procs.values())

    try:
        decision   = await analyze_and_decide(any_agent_running=any_running)
        action     = decision.get("action", "ATTENDRE")
        reason     = decision.get("reason", "")
        confidence = decision.get("confidence", "low")

        result = await execute_action(decision, running_procs)

        # Log dans autonomous_log.json
        entry = {
            "date":       _now_utc().isoformat(),
            "action":     action,
            "reason":     reason,
            "confidence": confidence,
            "result":     result,
        }
        append_autonomous_log(entry)

        # Apprentissage dans long_term.json (hors ATTENDRE silencieux)
        if action not in ("ATTENDRE",) or not result.startswith("Rien"):
            lt = _load_json(LONG_TERM_FILE, {})
            if "actions_autonomes" not in lt:
                lt["actions_autonomes"] = []
            lt["actions_autonomes"].append({
                "date":        _now_utc().isoformat(),
                "action":      action,
                "reason":      reason,
                "result":      result,
                "to_evaluate": action not in ("ATTENDRE", "ALERTER"),
            })
            lt["actions_autonomes"] = lt["actions_autonomes"][-50:]
            _save_json(LONG_TERM_FILE, lt)

    except Exception as e:
        print(f"[autonomous] Erreur tick : {e}")


# ─── COMMANDES TELEGRAM ───────────────────────────────────────────────────────

async def cmd_autonome(update, context):
    """/autonome — Active ou désactive la boucle autonome."""
    if not _authorized(update):
        return

    current   = is_autonomous_mode()
    new_state = not current
    set_autonomous_mode(new_state)

    if new_state:
        msg = (
            f"🤖 *Mode autonome ACTIVÉ*\n\n"
            f"L'agent va analyser la situation toutes les heures et agir seul.\n"
            f"Limite : *{MAX_ACTIONS_PER_DAY} actions par jour*.\n"
            f"Tu recevras une notification pour chaque action prise.\n\n"
            f"Commandes : /decisions · /plan · /autonome (pour désactiver)"
        )
    else:
        msg = (
            "⛔ *Mode autonome DÉSACTIVÉ*\n\n"
            "L'agent reste en mode réactif — il répond à tes messages uniquement."
        )
    await _reply(update, msg)


async def cmd_decisions(update, context):
    """/decisions — Liste les 10 dernières décisions prises en autonome."""
    if not _authorized(update):
        return

    log = load_autonomous_log()
    if not log:
        await _reply(update, "Aucune décision autonome enregistrée pour l'instant.")
        return

    recent = log[-10:]
    action_emoji = {
        "SCRAPER": "🕷️", "ENRICHIR": "🔬", "NETTOYER": "🧹",
        "ALERTER": "🔔", "ATTENDRE": "💤",
    }
    lines = ["🤖 *10 dernières décisions autonomes*\n"]
    for e in reversed(recent):
        date   = e.get("date", "")[:16].replace("T", " ")
        action = e.get("action", "?")
        emoji  = action_emoji.get(action, "•")
        conf   = e.get("confidence", "?")
        reason = _esc(e.get("reason", "")[:80])
        result = _esc(e.get("result", "")[:50])
        lines.append(
            f"{emoji} *{action}* `{conf}` — {date}\n"
            f"  {reason}\n"
            f"  → {result}"
        )

    await _reply(update, "\n\n".join(lines))


async def cmd_plan(update, context):
    """/plan — Ce que l'agent prévoit de faire demain."""
    if not _authorized(update):
        return

    context_text = _build_context()
    log          = load_autonomous_log()
    recent_text  = "\n".join(
        f"- [{e.get('date','')[:10]}] {e.get('action','')} : {e.get('reason','')[:80]}"
        for e in log[-5:]
    ) or "Aucune action récente."

    prompt = f"""Tu es CroustyLobster. Un fondateur te demande ce que tu prévois de faire demain en mode autonome.

Contexte actuel :
{context_text}

Tes actions récentes :
{recent_text}

Réponds en 3-5 lignes : ce que tu prévois de faire demain et pourquoi.
Direct, concis, en français, tutoie."""

    try:
        resp = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        plan = resp.content[0].text.strip()
    except Exception as e:
        plan = f"Impossible de générer le plan : {e}"

    mode_label = "✅ activé" if is_autonomous_mode() else "⛔ désactivé"
    await _reply(update, f"🗓️ *Plan pour demain* _(mode autonome : {mode_label})_\n\n{plan}")
