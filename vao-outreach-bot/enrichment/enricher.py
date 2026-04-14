"""
Orchestrateur d'enrichissement.
Pour chaque prospect : visite le site, analyse, score, met à jour Supabase.
"""

from __future__ import annotations

import asyncio
import logging
import json

from enrichment.site_analyzer import analyze_site
from config.scoring import compute_score
from outreach.stealth import get_stealth_context_options, random_delay
from services.playwright_manager import new_page, shutdown
from db.client import (
    get_prospects_to_enrich,
    mark_enriched,
    mark_scored,
    mark_no_form,
    mark_enrichment_failed,
    update_prospect,
)

log = logging.getLogger("vao.enricher")


async def enrich_one(prospect: dict) -> dict:
    """
    Enrichit un prospect : visite le site, analyse, retourne les données.
    Ne fait PAS d'écriture DB (le caller s'en charge).
    """
    pid = prospect["id"]
    name = prospect.get("company_name", "?")
    website = prospect.get("website", "")
    log.info("[%s] %s — %s", pid[:8], name, website)

    stealth = get_stealth_context_options()

    async with new_page(stealth) as page:
        analysis = await analyze_site(page, website)

    if analysis.error:
        log.warning("[%s] Erreur site: %s", pid[:8], analysis.error)

    return {
        "has_contact_form": analysis.has_contact_form,
        "contact_form_url": analysis.contact_form_url or None,
        "form_html_snapshot": analysis.form_html or None,
        "form_fields_mapping": json.dumps(analysis.form_fields_mapping) if analysis.form_fields_mapping else None,
        "site_keywords": analysis.site_keywords or None,
        "activity_types": analysis.activity_types or None,
        "site_quality_score": analysis.site_quality_score,
        "has_portfolio": analysis.has_portfolio,
    }


async def enrich_and_score(prospect: dict) -> str:
    """
    Enrichit + score un prospect. Met à jour la DB.
    Retourne le statut final : 'enriched', 'scored', 'no_form', 'enrichment_failed'.
    """
    pid = prospect["id"]
    name = prospect.get("company_name", "?")

    try:
        data = await enrich_one(prospect)
    except Exception as e:
        log.error("[%s] %s — échec enrichissement: %s", pid[:8], name, e)
        mark_enrichment_failed(pid, str(e))
        return "enrichment_failed"

    # Pas de formulaire de contact → marquer et passer
    if not data["has_contact_form"]:
        log.info("[%s] %s — pas de formulaire", pid[:8], name)
        mark_no_form(pid)
        return "no_form"

    # Sauvegarder l'enrichissement
    mark_enriched(pid, data)

    # Scorer immédiatement (on a toutes les infos)
    enriched_prospect = {**prospect, **data}
    score, tier, details = compute_score(enriched_prospect)

    if tier is None:
        log.info("[%s] %s — score %.1f → exclu", pid[:8], name, score)
        update_prospect(pid, {
            "outreach_score": score,
            "scoring_details": json.dumps(details),
            "campaign_status": "enriched",  # enrichi mais pas éligible
        })
        return "enriched"

    mark_scored(pid, score, tier, details)
    log.info("[%s] %s — score %.1f → tier %d ✓", pid[:8], name, score, tier)
    return "scored"


async def run_enrichment(limit: int = 100, delay_between: float | None = None) -> dict:
    """
    Lance l'enrichissement sur N prospects.
    Retourne les stats {total, scored, no_form, failed, enriched}.
    """
    prospects = get_prospects_to_enrich(limit)
    if not prospects:
        log.info("Aucun prospect à enrichir.")
        return {"total": 0}

    log.info("Enrichissement de %d prospects...", len(prospects))

    stats = {"total": len(prospects), "scored": 0, "no_form": 0, "failed": 0, "enriched": 0}

    for i, prospect in enumerate(prospects, 1):
        status = await enrich_and_score(prospect)
        if status in stats:
            stats[status] += 1
        elif status == "enrichment_failed":
            stats["failed"] += 1

        log.info("Progression: %d/%d", i, len(prospects))

        # Pause entre les prospects
        if i < len(prospects):
            wait = delay_between if delay_between is not None else random_delay("between_prospects")
            await asyncio.sleep(wait)

    log.info(
        "Enrichissement terminé — %d total, %d scorés, %d no_form, %d échecs",
        stats["total"], stats["scored"], stats["no_form"], stats["failed"],
    )

    await shutdown()
    return stats
