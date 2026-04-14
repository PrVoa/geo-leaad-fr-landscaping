"""Client Supabase — wrapper CRUD pour le bot outreach."""

from __future__ import annotations

import json
import datetime
from typing import Any

from supabase import create_client, Client

from config.settings import SUPABASE_URL, SUPABASE_SERVICE_KEY


_client: Client | None = None


def get_client() -> Client:
    """Singleton Supabase (service_role pour bypass RLS)."""
    global _client
    if _client is None:
        if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
            raise RuntimeError("SUPABASE_URL et SUPABASE_SERVICE_KEY requis dans .env")
        _client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    return _client


# ── Prospects (landscapers) ─────────────────────────────────────────────────

def get_prospects_to_enrich(limit: int = 100) -> list[dict]:
    """Prospects avec un site web, pas encore enrichis pour l'outreach."""
    return (
        get_client()
        .table("landscapers")
        .select("id, company_name, website, city, dept, region, phone, email, "
                "prenom_gerant, nom_gerant, siret, forme_juridique")
        .is_("campaign_status", "null")
        .not_.is_("website", "null")
        .neq("website", "")
        .limit(limit)
        .execute()
        .data
    )


def get_prospects_to_score(limit: int = 500) -> list[dict]:
    """Prospects enrichis en attente de scoring."""
    return (
        get_client()
        .table("landscapers")
        .select("*")
        .eq("campaign_status", "enriched")
        .limit(limit)
        .execute()
        .data
    )


def get_prospects_to_send(limit: int = 50) -> list[dict]:
    """Prospects éligibles à l'envoi aujourd'hui."""
    today = datetime.date.today().isoformat()
    return (
        get_client()
        .table("landscapers")
        .select("*")
        .in_("campaign_status", ["scored", "in_sequence"])
        .lte("next_action_date", today)
        .in_("next_action_type", ["send_form", "send_email"])
        .order("outreach_score", desc=True)
        .limit(limit)
        .execute()
        .data
    )


def get_call_list() -> list[dict]:
    """Prospects Tier 1 à appeler aujourd'hui."""
    today = datetime.date.today().isoformat()
    return (
        get_client()
        .table("landscapers")
        .select("id, prenom_gerant, nom_gerant, company_name, phone, city, "
                "website, outreach_score, activity_types")
        .eq("tier", 1)
        .eq("next_action_type", "call")
        .lte("next_action_date", today)
        .not_.in_("campaign_status", ["opted_out", "not_interested", "demo_booked", "converted"])
        .order("outreach_score", desc=True)
        .execute()
        .data
    )


def update_prospect(prospect_id: str, data: dict) -> None:
    """Met à jour un prospect avec les champs fournis."""
    data["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    get_client().table("landscapers").update(data).eq("id", prospect_id).execute()


def mark_enriched(prospect_id: str, enrichment_data: dict) -> None:
    """Met à jour un prospect après enrichissement."""
    enrichment_data["campaign_status"] = "enriched"
    enrichment_data["enriched_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    update_prospect(prospect_id, enrichment_data)


def mark_scored(prospect_id: str, score: float, tier: int, details: dict) -> None:
    """Met à jour un prospect après scoring."""
    update_prospect(prospect_id, {
        "outreach_score": score,
        "tier": tier,
        "scoring_details": json.dumps(details),
        "campaign_status": "scored",
        "scored_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "next_action_date": datetime.date.today().isoformat(),
        "next_action_type": "send_form",
    })


def mark_no_form(prospect_id: str) -> None:
    update_prospect(prospect_id, {"campaign_status": "no_form", "has_contact_form": False})


def mark_enrichment_failed(prospect_id: str, error: str) -> None:
    update_prospect(prospect_id, {
        "campaign_status": "enrichment_failed",
        "enriched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    })


# ── Submissions ─────────────────────────────────────────────────────────────

def create_submission(data: dict) -> dict:
    """Crée une soumission et retourne l'enregistrement créé."""
    return get_client().table("submissions").insert(data).execute().data[0]


def update_submission(submission_id: str, data: dict) -> None:
    get_client().table("submissions").update(data).eq("id", submission_id).execute()


def get_last_submission(prospect_id: str) -> dict | None:
    """Dernière soumission réussie pour un prospect."""
    rows = (
        get_client()
        .table("submissions")
        .select("*")
        .eq("prospect_id", prospect_id)
        .eq("status", "success")
        .order("sequence_step", desc=True)
        .limit(1)
        .execute()
        .data
    )
    return rows[0] if rows else None


# ── Responses ───────────────────────────────────────────────────────────────

def create_response(data: dict) -> dict:
    return get_client().table("responses").insert(data).execute().data[0]


# ── Call log ────────────────────────────────────────────────────────────────

def log_call(data: dict) -> dict:
    return get_client().table("call_log").insert(data).execute().data[0]


# ── Campaign config ─────────────────────────────────────────────────────────

def get_config(key: str, default: Any = None) -> Any:
    """Lit une valeur de config."""
    rows = (
        get_client()
        .table("campaign_config")
        .select("value")
        .eq("key", key)
        .limit(1)
        .execute()
        .data
    )
    if not rows:
        return default
    return rows[0]["value"]


def set_config(key: str, value: Any) -> None:
    """Écrit une valeur de config (upsert)."""
    get_client().table("campaign_config").upsert({
        "key": key,
        "value": json.dumps(value) if not isinstance(value, str) else value,
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }).execute()


# ── Stats ───────────────────────────────────────────────────────────────────

def get_pipeline_stats() -> list[dict]:
    """Stats pipeline par statut et tier (via la vue v_pipeline)."""
    return get_client().table("v_pipeline").select("*").execute().data
