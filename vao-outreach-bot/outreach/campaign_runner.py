"""
Orchestrateur principal de campagne.
Exécuté quotidiennement : récupère les prospects éligibles, envoie les messages,
met à jour les statuts dans Supabase.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import random
import signal
import time
from dataclasses import dataclass, field

import resend

from config.settings import (
    DAILY_SEND_LIMIT, SEND_DAYS, RESEND_API_KEY, RESEND_FROM,
    SENDER_NAME, SENDER_PHONE,
)
from config.sequences import get_step, next_step, SEQUENCE
from db.client import (
    get_prospects_to_send, update_prospect, create_submission,
    update_submission, get_last_submission,
)
from outreach.message_builder import build_message
from outreach.form_filler import fill_and_submit
from outreach.stealth import random_delay
from services.playwright_manager import shutdown as pw_shutdown

log = logging.getLogger("vao.campaign")


@dataclass
class DailyReport:
    date: str = ""
    total_attempted: int = 0
    successes: int = 0
    failures: int = 0
    captchas: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"=== Rapport campagne {self.date} ===",
            f"Tentatives : {self.total_attempted}",
            f"Succès     : {self.successes}",
            f"Échecs     : {self.failures}",
            f"CAPTCHAs   : {self.captchas}",
            f"Taux       : {self.successes / max(self.total_attempted, 1) * 100:.0f}%",
        ]
        if self.errors:
            lines.append(f"Erreurs    : {len(self.errors)}")
            for e in self.errors[:5]:
                lines.append(f"  - {e}")
        return "\n".join(lines)


# Graceful shutdown
_shutdown_requested = False


def _handle_signal(signum, frame):
    global _shutdown_requested
    log.info("Signal %s reçu — arrêt après le prospect en cours", signum)
    _shutdown_requested = True


def _is_send_day() -> bool:
    """Vérifie si aujourd'hui est un jour d'envoi."""
    today = datetime.date.today().strftime("%A").lower()
    return today in SEND_DAYS


def _pick_variant(step_config: dict) -> str:
    """Choisit un variant A/B aléatoirement parmi les disponibles."""
    variants = step_config.get("variants", ["A"])
    return random.choice(variants)


def _compute_next_action(prospect: dict, current_step: int, tier: int | None) -> dict:
    """
    Détermine la prochaine action pour un prospect après un envoi réussi.
    Retourne un dict de champs à mettre à jour dans le prospect.
    """
    step_config = get_step(current_step)
    nxt = next_step(current_step)

    updates: dict = {
        "current_sequence_step": current_step,
        "campaign_status": "in_sequence",
    }

    # Tier 1 : planifier un appel ?
    call_after = step_config.get("call_after_days") if step_config else None
    if tier == 1 and call_after and prospect.get("phone"):
        updates["next_action_type"] = "call"
        updates["next_action_date"] = (
            datetime.date.today() + datetime.timedelta(days=call_after)
        ).isoformat()
        return updates

    # Step suivant ?
    if nxt:
        delay = nxt["delay_days_after_previous"]
        updates["next_action_type"] = "send_form" if nxt["channel"] == "contact_form" else "send_email"
        updates["next_action_date"] = (
            datetime.date.today() + datetime.timedelta(days=delay)
        ).isoformat()
    else:
        # Séquence terminée
        updates["campaign_status"] = "sequence_complete"
        updates["next_action_type"] = None
        updates["next_action_date"] = None

    return updates


async def _send_email(prospect: dict, message_body: str, subject: str) -> dict:
    """Envoie un email direct via Resend (steps 4-5)."""
    email = prospect.get("email")
    if not email:
        return {"success": False, "error": "Pas d'email pour le prospect"}

    try:
        resend.api_key = RESEND_API_KEY
        result = resend.Emails.send({
            "from": f"{SENDER_NAME} <{RESEND_FROM}>",
            "to": [email],
            "subject": subject,
            "text": message_body,
        })
        return {"success": True, "resend_id": result.get("id")}
    except Exception as e:
        log.error("Erreur Resend pour %s: %s", prospect.get("id"), e)
        return {"success": False, "error": str(e)}


async def _process_prospect(prospect: dict, report: DailyReport) -> None:
    """Traite un prospect : construit le message, envoie, met à jour la DB."""
    prospect_id = prospect["id"]
    current_step = (prospect.get("current_sequence_step") or 0) + 1
    tier = prospect.get("tier")
    action_type = prospect.get("next_action_type", "send_form")

    step_config = get_step(current_step)
    if not step_config:
        log.warning("[%s] Step %d inexistant, skip", prospect_id, current_step)
        return

    channel = step_config["channel"]
    variant = _pick_variant(step_config)

    # Construire le message
    msg = build_message(prospect, current_step, variant)
    if msg.warnings:
        log.warning("[%s] Warnings message step %d: %s", prospect_id, current_step, msg.warnings)

    # Créer la soumission en DB
    submission = create_submission({
        "prospect_id": prospect_id,
        "sequence_step": current_step,
        "message_variant": variant,
        "channel": channel,
        "message_sent": msg.body,
        "subject_sent": msg.subject,
        "sender_name": SENDER_NAME,
        "sender_email": RESEND_FROM,
        "status": "in_progress",
        "attempted_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    })
    submission_id = submission["id"]

    report.total_attempted += 1

    # Envoyer selon le canal
    if channel == "contact_form":
        result = await fill_and_submit(prospect, msg.body, msg.subject)
        status = result.status
        error = result.error

        update_submission(submission_id, {
            "status": status,
            "error_details": error,
            "page_load_time_ms": result.page_load_ms,
            "form_fill_time_ms": result.form_fill_ms,
            "screenshot_path": result.screenshot_path,
            "completed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        })

    elif channel == "email":
        email_result = await _send_email(prospect, msg.body, msg.subject or "")
        if email_result["success"]:
            status = "success"
            error = None
        else:
            status = "failed_other"
            error = email_result.get("error")

        update_submission(submission_id, {
            "status": status,
            "error_details": error,
            "completed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        })

    else:
        log.error("[%s] Canal inconnu: %s", prospect_id, channel)
        return

    # Mettre à jour le prospect selon le résultat
    if status == "success":
        report.successes += 1
        next_action = _compute_next_action(prospect, current_step, tier)
        update_prospect(prospect_id, next_action)
        log.info("[%s] Step %d/%s envoyé OK", prospect_id, current_step, variant)
    else:
        if status == "failed_captcha":
            report.captchas += 1
        report.failures += 1
        report.errors.append(f"{prospect.get('company_name', prospect_id)}: {status} — {error}")
        log.warning("[%s] Step %d échoué: %s — %s", prospect_id, current_step, status, error)

        # Retry au prochain cycle sans avancer le step
        if status in ("failed_timeout", "failed_blocked", "failed_other"):
            update_prospect(prospect_id, {
                "next_action_date": (
                    datetime.date.today() + datetime.timedelta(days=1)
                ).isoformat(),
            })


async def run_campaign(limit: int | None = None, force: bool = False) -> DailyReport:
    """
    Exécute la campagne du jour.

    Args:
        limit: nombre max de prospects à traiter (défaut: DAILY_SEND_LIMIT)
        force: ignorer la vérification du jour d'envoi
    """
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    report = DailyReport(date=datetime.date.today().isoformat())

    if not force and not _is_send_day():
        log.info("Pas un jour d'envoi (%s), arrêt", datetime.date.today().strftime("%A"))
        return report

    send_limit = limit or DAILY_SEND_LIMIT
    prospects = get_prospects_to_send(send_limit)

    if not prospects:
        log.info("Aucun prospect éligible aujourd'hui")
        return report

    log.info("Campagne lancée — %d prospects à traiter", len(prospects))

    for i, prospect in enumerate(prospects):
        if _shutdown_requested:
            log.info("Arrêt demandé après %d/%d prospects", i, len(prospects))
            break

        try:
            await _process_prospect(prospect, report)
        except Exception as e:
            log.exception("[%s] Erreur non gérée", prospect.get("id"))
            report.failures += 1
            report.errors.append(f"{prospect.get('company_name', '?')}: exception — {e}")

        # Délai entre prospects (60-120s) sauf le dernier
        if i < len(prospects) - 1 and not _shutdown_requested:
            delay = random_delay("between_prospects")
            log.debug("Pause %.0fs avant le prochain prospect", delay)
            await asyncio.sleep(delay)

    # Cleanup Playwright
    try:
        await pw_shutdown()
    except Exception:
        pass

    log.info(report.summary())
    return report
