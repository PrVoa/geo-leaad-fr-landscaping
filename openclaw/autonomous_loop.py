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

# Pipeline scraper → enrich → clean
PIPELINE_STAGES = [
    ("scraper", SCRAPER_CMD),
    ("enrich",  ENRICH_CMD),
    ("clean",   CLEAN_CMD),
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


# ─── PIPELINE MANAGER ────────────────────────────────────────────────────────

def _sync_pipeline_state_from_business():
    """
    Infère l'état du pipeline depuis business.json pour éviter les faux 'jamais lancé'.
    À appeler AVANT pipeline_advance() pour que les conditions de déclenchement soient correctes.
    """
    business = _load_json(BUSINESS_FILE, {})
    try:
        leads_total = int(business.get("leads_total", 0) or 0)
        enrichis    = int(business.get("enrichis",    0) or 0)
    except (ValueError, TypeError):
        return

    alert   = _load_json(ALERT_FILE, {})
    changed = False

    if leads_total > 1000 and not alert.get("last_scraper_finished"):
        alert["last_scraper_finished"]  = _now_utc().isoformat()
        alert["last_scraper_exit_code"] = 0
        changed = True
        print(f"[PIPELINE] Inférence : scraper terminé ({leads_total} leads en base) → alert_state mis à jour")

    if enrichis > 0 and not alert.get("last_enrich_finished"):
        alert["last_enrich_finished"]  = _now_utc().isoformat()
        alert["last_enrich_exit_code"] = 0
        changed = True
        print(f"[PIPELINE] Inférence : enrich terminé ({enrichis} enrichis en base) → alert_state mis à jour")

    if changed:
        _save_json(ALERT_FILE, alert)


def _record_stage_started(stage: str):
    """Marque qu'un stage a démarré (réinitialise la fin précédente)."""
    alert = _load_json(ALERT_FILE, {})
    alert[f"last_{stage}_started"]   = _now_utc().isoformat()
    alert.pop(f"last_{stage}_finished",  None)
    alert.pop(f"last_{stage}_exit_code", None)
    _save_json(ALERT_FILE, alert)

def _record_stage_finished(stage: str, exit_code: int):
    """Marque qu'un stage s'est terminé avec un code de sortie."""
    alert = _load_json(ALERT_FILE, {})
    if not alert.get(f"last_{stage}_finished"):  # ne pas écraser si déjà noté
        alert[f"last_{stage}_finished"]  = _now_utc().isoformat()
        alert[f"last_{stage}_exit_code"] = exit_code
        _save_json(ALERT_FILE, alert)

def _pipeline_next_needed(from_stage: str, to_stage: str) -> bool:
    """
    True si to_stage doit être lancé après la fin de from_stage.
    Critères : from_stage terminé OK + (to_stage jamais lancé OU lancé avant from_stage).
    """
    alert = _load_json(ALERT_FILE, {})
    from_finished = alert.get(f"last_{from_stage}_finished")
    from_exit     = alert.get(f"last_{from_stage}_exit_code")
    to_started    = alert.get(f"last_{to_stage}_started")

    if from_exit != 0 or not from_finished:
        return False
    if not to_started:
        return True
    return to_started < from_finished  # to_stage a démarré avant la fin de from_stage

async def pipeline_advance(running_procs: dict) -> bool:
    """
    Vérifie si un stage du pipeline vient de terminer et lance le suivant.
    Appelé à chaque tick avant analyze_and_decide.
    Couvre aussi le cas post-restart (proc non tracké mais alert_state à jour).
    Retourne True si une action a été prise.
    """
    # Synchronise l'état depuis business.json (évite faux "jamais lancé")
    _sync_pipeline_state_from_business()

    for i, (stage, _) in enumerate(PIPELINE_STAGES[:-1]):
        proc = running_procs.get(stage)
        next_stage, next_cmd = PIPELINE_STAGES[i + 1]

        print(f"[PIPELINE] Vérification {stage.upper()} → {next_stage.upper()}...")

        # Encore en cours dans cette session → skip
        if proc is not None and proc.poll() is None:
            print(f"[PIPELINE] {stage.upper()} toujours en cours (PID {proc.pid}) — skip")
            continue

        # Process suivi ET terminé → enregistrer la fin
        if proc is not None:
            exit_code = proc.returncode
            print(f"[PIPELINE] {stage.upper()} terminé (exit {exit_code}) — enregistrement")
            _record_stage_finished(stage, exit_code)
            if exit_code != 0:
                alert = _load_json(ALERT_FILE, {})
                crash_key = f"{stage}_crash_alerted"
                if not alert.get(crash_key):
                    alert[crash_key] = _now_utc().isoformat()
                    _save_json(ALERT_FILE, alert)
                    await _broadcast(
                        f"❌ *Pipeline — {stage.upper()} a crashé*\n"
                        f"Code de sortie : `{exit_code}`\n"
                        f"Quentin, vérifiez les logs avec /logs."
                    )
                continue

        # Vérifier si l'étape suivante est nécessaire
        # (process suivi ou non — couvre le cas post-restart où proc=None)
        needed = _pipeline_next_needed(stage, next_stage)
        print(f"[PIPELINE] {next_stage.upper()} nécessaire ? {'OUI' if needed else 'non'}")

        if not needed:
            continue  # déjà avancé

        next_proc = running_procs.get(next_stage)
        if next_proc and next_proc.poll() is None:
            print(f"[PIPELINE] {next_stage.upper()} déjà en cours (PID {next_proc.pid}) — skip")
            continue  # déjà en cours

        if count_actions_today() >= MAX_ACTIONS_PER_DAY:
            print(f"[PIPELINE] Quota journalier atteint — {next_stage.upper()} en attente")
            await _broadcast(
                f"⚠️ *Pipeline en attente* — quota journalier atteint.\n"
                f"Prochaine étape : *{next_stage.upper()}* (sera lancée demain)."
            )
            return False

        try:
            print(f"[PIPELINE] Lancement {next_stage.upper()}... ({next_cmd[-1]})")
            proc_next = subprocess.Popen(next_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            running_procs[next_stage] = proc_next
            _record_stage_started(next_stage)

            result = f"{next_stage} lancé (PID {proc_next.pid})"
            print(f"[PIPELINE] ✅ {result}")
            await _broadcast(
                f"🤖 *Pipeline → {next_stage.upper()}*\n"
                f"_{stage.upper()} terminé normalement — étape suivante démarrée automatiquement_\n\n"
                f"✅ {result}"
            )
            append_autonomous_log({
                "date":       _now_utc().isoformat(),
                "action":     next_stage.upper(),
                "reason":     f"Pipeline automatique : {stage} terminé (exit 0)",
                "confidence": "high",
                "result":     result,
            })
            return True
        except Exception as e:
            print(f"[PIPELINE] ❌ Erreur lancement {next_stage} : {e}")
            await _broadcast(f"❌ Erreur pipeline {next_stage} : {e}")

    return False


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

_TG_MAX = 4096

def _split_text(text: str, limit: int = _TG_MAX) -> list:
    if len(text) <= limit:
        return [text]
    parts = []
    while text:
        if len(text) <= limit:
            parts.append(text)
            break
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return parts


async def _send(chat_id: str, text: str):
    bot = Bot(token=BOT_TOKEN)
    for part in _split_text(text):
        try:
            await bot.send_message(chat_id=chat_id, text=part, parse_mode="Markdown")
        except Exception:
            clean = re.sub(r'[*_`\[\]]', '', part)
            try:
                await bot.send_message(chat_id=chat_id, text=clean)
            except Exception:
                pass

async def _broadcast(text: str):
    for cid in _CHAT_IDS:
        await _send(cid, text)

async def _reply(update, text: str):
    for part in _split_text(text):
        try:
            await update.message.reply_text(part, parse_mode="Markdown")
        except Exception:
            clean = re.sub(r'[*_`\[\]\\]', '', part)
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

    # État pipeline détaillé
    def _stage_status(stage: str) -> str:
        proc_running = any_agent_running  # simplifié — raffiné ci-dessous
        a = _load_json(ALERT_FILE, {})
        finished  = a.get(f"last_{stage}_finished")
        exit_code = a.get(f"last_{stage}_exit_code")
        started   = a.get(f"last_{stage}_started")
        if finished:
            h_ago = round((_now_utc() - datetime.datetime.fromisoformat(finished)).total_seconds() / 3600, 1)
            if exit_code == 0:
                return f"terminé normalement il y a {h_ago}h"
            else:
                return f"CRASH (code {exit_code}) il y a {h_ago}h"
        if started:
            h_ago = round((_now_utc() - datetime.datetime.fromisoformat(started)).total_seconds() / 3600, 1)
            return f"démarré il y a {h_ago}h (peut-être en cours)"
        return "jamais lancé dans cette session"

    # Déduction de l'état réel depuis les données business
    leads_total = business.get("leads_total", 0) or 0
    enrichis    = business.get("enrichis",    0) or 0
    try:
        leads_total = int(leads_total)
        enrichis    = int(enrichis)
    except (ValueError, TypeError):
        leads_total = 0
        enrichis    = 0

    scraper_inferred = leads_total > 1000   # forcément terminé si leads en base
    enrich_inferred  = enrichis > 0

    # Si le pipeline tracking n'a pas d'historique mais les données prouvent que ça a tourné,
    # on injecte des timestamps fictifs pour éviter les faux "jamais lancé"
    alert_current = _load_json(ALERT_FILE, {})
    if scraper_inferred and not alert_current.get("last_scraper_finished"):
        alert_current["last_scraper_finished"]  = _now_utc().isoformat()
        alert_current["last_scraper_exit_code"] = 0
        _save_json(ALERT_FILE, alert_current)
    if enrich_inferred and not alert_current.get("last_enrich_finished"):
        alert_current["last_enrich_finished"]  = _now_utc().isoformat()
        alert_current["last_enrich_exit_code"] = 0
        _save_json(ALERT_FILE, alert_current)

    parts.append(f"\n=== ÉTAT INFRA ===")
    parts.append(f"Dernier scraper actif : {last_scraper}")
    for stage, _ in PIPELINE_STAGES:
        parts.append(f"  {stage:8s}: {_stage_status(stage)}")
    parts.append(f"Agent en cours        : {'OUI' if any_agent_running else 'non'}")
    parts.append(f"Actions prises aujourd'hui : {count_actions_today()}/{MAX_ACTIONS_PER_DAY}")

    # Diagnostic clair pour Claude — empêche les fausses alertes
    if leads_total > 1000:
        parts.append(f"\n=== ÉTAT RÉEL (PRIORITAIRE) ===")
        parts.append(f"✅ {leads_total} leads en base → scraper a terminé normalement. INFRA SAINE.")
    if enrichis > 0:
        parts.append(f"✅ {enrichis} leads enrichis → enrichissement a tourné. C'est NORMAL.")

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
Tu tournes en mode autonome. Tu es le MANAGER du pipeline de leads : scraper → enrichir → nettoyer.
Le pipeline avance automatiquement entre les étapes — tu n'as PAS besoin de relancer une étape qui vient de terminer normalement.

Contexte :
{context}

RÈGLES ANTI-FAUSSES-ALERTES (PRIORITAIRES) :
- Si leads_total > 1000 → le scraper a DÉJÀ terminé normalement. NE PAS alerter "infrastructure paralysée" ou "scraper jamais lancé". C'est un état NORMAL.
- Si enrichis > 0 → l'enrichissement a déjà tourné. NE PAS alerter à ce sujet.
- "jamais lancé dans cette session" = le bot vient de redémarrer, pas que le scraper n'a jamais tourné.
- Si leads_total > 1000, ne JAMAIS choisir ALERTER pour motif lié au scraper ou à l'infra.
- L'état actuel avec 21 811 leads est l'état NORMAL et SAIN du projet.

IMPORTANT sur le pipeline :
- Si un stage affiche "terminé normalement" → c'est NORMAL, l'infra n'est PAS bloquée.
- Le pipeline avance automatiquement : scraper terminé → enrich se lance seul → clean se lance seul.
- Ne lance SCRAPER que si toutes les zones ont été couvertes ET que tu as la preuve de nouvelles zones à scraper.
- Ne lance PAS SCRAPER juste parce que le scraper est "inactif depuis Xh".

Tu dois choisir UNE action :
- SCRAPER   : relancer le scraping (nouvelles zones à couvrir, pipeline complet terminé)
- ENRICHIR  : lancer l'enrichissement si scraper terminé OK mais enrich pas fait récemment
- NETTOYER  : lancer le nettoyage si enrich terminé OK mais clean pas fait récemment
- ALERTER   : prévenir Quentin si situation critique, crash, ou hésitation
- ATTENDRE  : pipeline en cours, ou situation normale, rien à faire

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
            _record_stage_started("scraper")
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
            _record_stage_started("enrich")
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
            _record_stage_started("clean")
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

    # Pipeline manager : avance automatiquement scraper → enrich → clean
    try:
        pipeline_acted = await pipeline_advance(running_procs)
    except Exception as e:
        print(f"[autonomous] Erreur pipeline_advance : {e}")
        pipeline_acted = False

    any_running = any(p.poll() is None for p in running_procs.values())

    # Si le pipeline a déjà pris une action, pas besoin de consulter Claude ce tick
    if pipeline_acted:
        append_autonomous_log({
            "date":       _now_utc().isoformat(),
            "action":     "PIPELINE",
            "reason":     "Avancement automatique du pipeline",
            "confidence": "high",
            "result":     "étape suivante lancée",
        })
        return

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
