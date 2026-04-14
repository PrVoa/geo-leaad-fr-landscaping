"""
Règles de scoring ICP pour les paysagistes.
Score total = somme des points. Max theorique ~ 12.
Tier 1 : score >= 5  (mail + appel)
Tier 2 : score 2-4   (mail uniquement)
Exclu  : score < 2
"""

URBAN_DEPARTMENTS = [
    "75", "92", "93", "94", "78", "91", "95", "77",  # Île-de-France
    "69", "13", "31", "33", "59", "44", "67", "06",  # Grandes métropoles
    "34", "35", "38", "42", "54", "57", "76",         # Villes moyennes+
]

HIGH_MARGIN_ACTIVITIES = {"creation_jardin", "amenagement", "piscine", "terrasse"}
MEDIUM_MARGIN_ACTIVITIES = {"entretien", "elagage", "taille"}
LOW_VALUE_ONLY = {"tonte", "petit_entretien"}

TIER_THRESHOLDS = {
    "tier_1": 5.0,
    "tier_2": 2.0,
}


def compute_score(prospect: dict) -> tuple[float, int | None, dict]:
    """
    Calcule le score ICP d'un prospect.
    Retourne (score, tier, details).
    tier = 1, 2 ou None (exclu).
    """
    score = 0.0
    details: dict[str, float] = {}

    # Qualité du site web
    sq = prospect.get("site_quality_score") or 0
    if sq >= 5:
        score += 2.0
        details["has_professional_site"] = 2.0

    if prospect.get("has_contact_form"):
        score += 1.0
        details["has_contact_form"] = 1.0

    if prospect.get("has_portfolio"):
        score += 1.0
        details["has_portfolio"] = 1.0

    # Type d'activité
    activities = set(prospect.get("activity_types") or [])
    if activities & HIGH_MARGIN_ACTIVITIES:
        score += 2.0
        details["high_margin_services"] = 2.0
    elif activities & MEDIUM_MARGIN_ACTIVITIES:
        score += 1.0
        details["medium_margin_services"] = 1.0

    if activities and activities <= LOW_VALUE_ONLY:
        details["low_value_only"] = 0.0

    # Structure
    fj = (prospect.get("forme_juridique") or "").upper()
    small_structures = {"EI", "EIRL", "EURL", "SASU", "AUTO-ENTREPRENEUR", ""}
    if fj in small_structures or not fj:
        score += 1.0
        details["independent_or_small"] = 1.0

    keywords = prospect.get("site_keywords") or []
    if "architecte" in keywords:
        score += 0.5
        details["architect_paysagiste"] = 0.5

    if "bureau_etudes" in activities:
        score -= 1.0
        details["bureau_etudes"] = -1.0

    # Localisation
    dept = prospect.get("dept") or ""
    if dept in URBAN_DEPARTMENTS:
        score += 1.0
        details["zone_urbaine"] = 1.0

    # Contact
    if prospect.get("phone"):
        score += 0.5
        details["has_phone"] = 0.5

    if prospect.get("email"):
        score += 0.5
        details["has_email"] = 0.5

    # Tier
    tier: int | None = None
    if score >= TIER_THRESHOLDS["tier_1"]:
        tier = 1
    elif score >= TIER_THRESHOLDS["tier_2"]:
        tier = 2

    return score, tier, details
