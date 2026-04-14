"""
Analyse du site web d'un prospect paysagiste.
Extrait keywords, activity_types, qualité du site, portfolio, formulaire.
"""

from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field
from urllib.parse import urlparse

from playwright.async_api import Page

from enrichment.form_detector import detect_contact_form, FormDetectionResult
from outreach.field_mapper import map_form_fields, mapping_to_dict, FormMapping

log = logging.getLogger("vao.site_analyzer")


# ── Classification d'activités ─────────────────────────────────────────────

ACTIVITY_KEYWORDS: dict[str, list[str]] = {
    "creation_jardin": [
        "création de jardin", "creation jardin", "conception jardin",
        "aménagement paysager", "amenagement paysager",
        "création paysagère", "conception paysagère",
    ],
    "entretien": [
        "entretien", "tonte", "taille de haie", "taille haie",
        "débroussaillage", "debroussaillage", "désherbage",
    ],
    "elagage": [
        "élagage", "elagage", "abattage", "soin des arbres",
        "arboriste", "grimpeur", "haubanage",
    ],
    "terrasse": [
        "terrasse", "dallage", "pavage", "bois composite",
        "terrasse bois",
    ],
    "piscine": [
        "piscine", "bassin", "pièce d'eau", "baignade naturelle",
    ],
    "cloture": [
        "clôture", "cloture", "portail", "grillage", "palissade",
    ],
    "arrosage": [
        "arrosage automatique", "irrigation", "arrosage intégré",
    ],
    "architecte": [
        "architecte paysagiste", "bureau d'études", "maîtrise d'œuvre",
        "étude paysagère",
    ],
    "tonte": [
        "tonte pelouse", "tonte gazon",
    ],
    "amenagement": [
        "aménagement extérieur", "amenagement exterieur",
        "aménagement de jardin",
    ],
}

# Pages portfolio
PORTFOLIO_PATTERNS = re.compile(
    r"(r[eé]alisation|portfolio|galerie|projet|chantier|photo|nos.travaux)", re.I
)


@dataclass
class SiteAnalysis:
    """Résultat de l'analyse d'un site web."""
    site_keywords: list[str] = field(default_factory=list)
    activity_types: list[str] = field(default_factory=list)
    site_quality_score: int = 0
    has_portfolio: bool = False

    # Formulaire
    has_contact_form: bool = False
    contact_form_url: str = ""
    form_html: str = ""
    form_fields_mapping: dict | None = None
    form_has_captcha: bool = False

    # Méta
    error: str = ""


def _normalize(text: str) -> str:
    """Minuscule, retire accents fréquents pour matching."""
    return text.lower()


def _extract_activities(text: str) -> list[str]:
    """Classifie les activités détectées dans le texte."""
    text_lower = _normalize(text)
    found = []
    for activity, keywords in ACTIVITY_KEYWORDS.items():
        for kw in keywords:
            if kw.lower() in text_lower:
                found.append(activity)
                break
    return found


def _extract_keywords(title: str, description: str, headings: list[str]) -> list[str]:
    """Extrait les mots-clés pertinents du site."""
    all_text = f"{title} {description} {' '.join(headings)}"
    keywords = []

    kw_patterns = [
        "paysagiste", "paysage", "jardin", "jardinage",
        "élagage", "elagage", "entretien", "espaces verts",
        "terrasse", "piscine", "clôture", "arrosage",
        "aménagement", "creation", "conception",
        "architecte", "bureau d'études",
        "devis", "gratuit",
    ]

    text_lower = all_text.lower()
    for kw in kw_patterns:
        if kw in text_lower:
            keywords.append(kw)

    return list(set(keywords))


async def _eval_site_quality(page: Page, url: str) -> int:
    """Évalue la qualité du site (0-10)."""
    score = 0

    # HTTPS
    if url.startswith("https://"):
        score += 1

    # Contenu riche (au moins 200 mots visibles)
    try:
        text = await page.evaluate("document.body?.innerText || ''")
        word_count = len(text.split())
        if word_count > 200:
            score += 2
        elif word_count > 50:
            score += 1
    except Exception:
        pass

    # A un title
    try:
        title = await page.title()
        if title and len(title) > 5:
            score += 1
    except Exception:
        pass

    # A une meta description
    try:
        desc = await page.locator('meta[name="description"]').get_attribute("content")
        if desc and len(desc) > 20:
            score += 1
    except Exception:
        pass

    # Images (un site pro en a au moins 3)
    try:
        img_count = await page.locator("img").count()
        if img_count >= 5:
            score += 2
        elif img_count >= 3:
            score += 1
    except Exception:
        pass

    # Viewport meta (responsive)
    try:
        vp = await page.locator('meta[name="viewport"]').count()
        if vp > 0:
            score += 1
    except Exception:
        pass

    # Navigation structurée (menu)
    try:
        nav = await page.locator("nav, .menu, .navbar, #menu, #nav").count()
        if nav > 0:
            score += 1
    except Exception:
        pass

    # Footer (site complet)
    try:
        footer = await page.locator("footer").count()
        if footer > 0:
            score += 1
    except Exception:
        pass

    return min(score, 10)


async def _detect_portfolio(page: Page) -> bool:
    """Détecte si le site a une page de réalisations/portfolio."""
    try:
        links = await page.locator("a").all()
        for link in links:
            href = await link.get_attribute("href") or ""
            text = await link.text_content() or ""
            if PORTFOLIO_PATTERNS.search(href) or PORTFOLIO_PATTERNS.search(text):
                return True
    except Exception:
        pass
    return False


async def analyze_site(page: Page, website: str) -> SiteAnalysis:
    """
    Analyse complète d'un site de paysagiste.
    - Extrait keywords et activity_types
    - Évalue la qualité du site
    - Détecte portfolio
    - Détecte et mappe le formulaire de contact
    """
    result = SiteAnalysis()
    base_url = website if website.startswith("http") else f"https://{website}"

    # 1. Charger la page d'accueil
    try:
        resp = await page.goto(base_url, wait_until="domcontentloaded", timeout=15_000)
        if not resp or not resp.ok:
            result.error = f"HTTP {resp.status if resp else 'no response'}"
            return result
    except Exception as e:
        result.error = str(e)[:200]
        return result

    # 2. Extraire les infos textuelles
    try:
        title = await page.title() or ""
        desc_el = await page.query_selector('meta[name="description"]')
        description = (await desc_el.get_attribute("content") or "") if desc_el else ""

        headings = []
        for tag in ["h1", "h2", "h3"]:
            els = await page.locator(tag).all()
            for el in els[:10]:  # max 10 par tag
                txt = await el.text_content()
                if txt:
                    headings.append(txt.strip())

        body_text = await page.evaluate("document.body?.innerText || ''")
    except Exception:
        title, description, headings, body_text = "", "", [], ""

    # 3. Keywords et activités
    result.site_keywords = _extract_keywords(title, description, headings)
    full_text = f"{title} {description} {' '.join(headings)} {body_text[:3000]}"
    result.activity_types = _extract_activities(full_text)

    # 4. Qualité du site
    result.site_quality_score = await _eval_site_quality(page, base_url)

    # 5. Portfolio
    result.has_portfolio = await _detect_portfolio(page)

    # 6. Formulaire de contact
    form_result: FormDetectionResult = await detect_contact_form(page, website)
    result.has_contact_form = form_result.found
    result.contact_form_url = form_result.form_url
    result.form_html = form_result.form_html
    result.form_has_captcha = form_result.has_captcha

    # 7. Mapper les champs si formulaire trouvé
    if form_result.found and form_result.form_selector:
        try:
            mapping: FormMapping = await map_form_fields(page, form_result.form_selector)
            if mapping.has_minimum():
                result.form_fields_mapping = mapping_to_dict(mapping)
            else:
                log.info("Formulaire trouvé mais mapping insuffisant (pas email+message)")
        except Exception as e:
            log.warning("Erreur mapping formulaire: %s", e)

    return result
