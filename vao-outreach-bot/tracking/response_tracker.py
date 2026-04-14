"""
Suivi des réponses par polling IMAP.
Matche les emails reçus aux prospects, classifie le sentiment et l'intent.
"""

from __future__ import annotations

import logging
import re
import time
from email import message_from_bytes

from imapclient import IMAPClient

from config.settings import IMAP_HOST, IMAP_USER, IMAP_PASS, has_imap
from db.client import (
    get_client, create_response, update_prospect,
)

log = logging.getLogger("vao.response_tracker")

# Mots-clés opt-out
OPT_OUT_PATTERNS = re.compile(
    r"\b(stop|désabonner|désinscri|plus de message|ne me contactez plus|spam|arrêt)\b",
    re.I,
)

# Mots-clés positifs
POSITIVE_PATTERNS = re.compile(
    r"\b(intéress|oui|d'accord|ok pour|je veux|montrez.moi|envoyer.moi|"
    r"essayer|tester|démo|rdv|rendez.vous|appeler.moi|rappeler)\b",
    re.I,
)

# Mots-clés négatifs
NEGATIVE_PATTERNS = re.compile(
    r"\b(pas intéress|non merci|non,?\s|pas besoin|déjà un|déjà équipé|"
    r"pas pour nous|pas pour moi|pas le moment)\b",
    re.I,
)


def _classify_response(body: str) -> tuple[str, str]:
    """
    Classifie une réponse en sentiment et intent.
    Returns: (sentiment, intent)
    """
    body_lower = body.lower()

    # Opt-out en priorité
    if OPT_OUT_PATTERNS.search(body_lower):
        return "opt_out", "stop"

    # Positif
    if POSITIVE_PATTERNS.search(body_lower):
        if re.search(r"\b(démo|essayer|tester|montrer)\b", body_lower, re.I):
            return "positive", "demo_request"
        return "positive", "question"

    # Négatif
    if NEGATIVE_PATTERNS.search(body_lower):
        return "negative", "not_interested"

    return "neutral", "other"


def _match_prospect(from_email: str, body: str) -> dict | None:
    """
    Matche un email reçu à un prospect.
    Cherche par email d'abord, puis par nom d'entreprise ou ville.
    """
    client = get_client()

    # 1. Match par email exact
    if from_email:
        rows = (
            client.table("landscapers")
            .select("id, company_name, campaign_status")
            .eq("email", from_email.lower())
            .limit(1)
            .execute()
            .data
        )
        if rows:
            return rows[0]

    # 2. Match par domaine email
    if from_email and "@" in from_email:
        domain = from_email.split("@")[1].lower()
        # Exclure les webmails
        if domain not in ("gmail.com", "yahoo.fr", "hotmail.fr", "hotmail.com",
                          "outlook.fr", "outlook.com", "orange.fr", "wanadoo.fr",
                          "free.fr", "sfr.fr", "laposte.net"):
            rows = (
                client.table("landscapers")
                .select("id, company_name, campaign_status")
                .ilike("email", f"%@{domain}")
                .limit(1)
                .execute()
                .data
            )
            if rows:
                return rows[0]

    return None


def _extract_email_body(raw_msg: bytes) -> tuple[str, str, str]:
    """
    Parse un email brut et retourne (from, subject, body_text).
    """
    msg = message_from_bytes(raw_msg)
    from_addr = msg.get("From", "")
    subject = msg.get("Subject", "")

    # Extraire l'adresse email du From
    match = re.search(r"<([^>]+)>", from_addr)
    if match:
        from_addr = match.group(1)

    # Extraire le body text
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    body = payload.decode(charset, errors="replace")
                    break
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            body = payload.decode(charset, errors="replace")

    return from_addr.strip().lower(), subject, body.strip()


def check_responses() -> int:
    """
    Vérifie les nouvelles réponses via IMAP.
    Retourne le nombre de réponses traitées.
    """
    if not has_imap():
        log.info("IMAP non configuré, skip")
        return 0

    processed = 0

    try:
        with IMAPClient(IMAP_HOST, ssl=True) as imap:
            imap.login(IMAP_USER, IMAP_PASS)
            imap.select_folder("INBOX")

            # Chercher les messages non lus
            messages = imap.search(["UNSEEN"])
            if not messages:
                log.debug("Pas de nouveaux emails")
                return 0

            log.info("%d nouveaux emails à traiter", len(messages))

            for uid in messages:
                try:
                    raw = imap.fetch([uid], ["RFC822"])
                    raw_msg = raw[uid][b"RFC822"]
                    from_addr, subject, body = _extract_email_body(raw_msg)

                    if not body and not subject:
                        continue

                    # Matcher au prospect
                    prospect = _match_prospect(from_addr, body)
                    if not prospect:
                        log.debug("Email de %s non matché à un prospect", from_addr)
                        continue

                    # Classifier
                    sentiment, intent = _classify_response(body)

                    # Enregistrer la réponse
                    create_response({
                        "prospect_id": prospect["id"],
                        "response_channel": "email",
                        "response_body": body[:5000],
                        "response_subject": subject[:500],
                        "sentiment": sentiment,
                        "intent": intent,
                    })

                    # Mettre à jour le prospect
                    status_update = {"campaign_status": "responded"}
                    if intent == "stop":
                        status_update["campaign_status"] = "opted_out"
                        status_update["next_action_type"] = None
                        status_update["next_action_date"] = None

                    update_prospect(prospect["id"], status_update)

                    log.info(
                        "Réponse de %s (%s) — sentiment=%s, intent=%s",
                        prospect.get("company_name", "?"), from_addr,
                        sentiment, intent,
                    )
                    processed += 1

                except Exception as e:
                    log.warning("Erreur traitement email uid=%s: %s", uid, e)

    except Exception as e:
        log.error("Erreur connexion IMAP: %s", e)

    return processed


def run_response_loop(interval: int = 1800) -> None:
    """Boucle de vérification des réponses (interval en secondes)."""
    log.info("Response tracker démarré — intervalle %ds", interval)
    while True:
        try:
            n = check_responses()
            if n:
                log.info("%d réponse(s) traitée(s)", n)
        except Exception as e:
            log.exception("Erreur dans la boucle response: %s", e)
        time.sleep(interval)
