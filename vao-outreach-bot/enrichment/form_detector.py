"""Détection de formulaires de contact sur les sites de paysagistes."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from playwright.async_api import Page, Locator


# Pages probables pour le formulaire de contact
CONTACT_PATHS = [
    "/contact", "/nous-contacter", "/contactez-nous", "/contact.html",
    "/nous-contacter.html", "/contactez-nous.html",
]

# Textes de liens vers la page contact
CONTACT_LINK_PATTERNS = re.compile(
    r"\b(contact|nous.contacter|contactez.nous|devis|demande)\b", re.IGNORECASE
)

# Sélecteurs de succès après soumission
SUCCESS_PATTERNS = re.compile(
    r"(merci|envoy[ée]|thank|success|reçu|bien.reçu|confirmé)", re.IGNORECASE
)


@dataclass
class FormDetectionResult:
    found: bool = False
    form_url: str = ""
    form_html: str = ""
    form_selector: str = ""
    field_count: int = 0
    has_textarea: bool = False
    has_captcha: bool = False


async def _find_form_on_page(page: Page) -> FormDetectionResult | None:
    """Cherche un formulaire de contact sur la page courante."""
    # Chercher tous les <form> visibles
    forms = await page.locator("form").all()

    for form in forms:
        try:
            if not await form.is_visible():
                continue
        except Exception:
            continue

        html = await form.inner_html()
        html_lower = html.lower()

        # Exclure les formulaires de recherche
        if "search" in html_lower and "message" not in html_lower:
            continue

        # Exclure les formulaires de newsletter (juste un email + submit)
        inputs = await form.locator("input:not([type=hidden]):not([type=submit])").all()
        textareas = await form.locator("textarea").all()

        visible_inputs = []
        for inp in inputs:
            try:
                if await inp.is_visible():
                    visible_inputs.append(inp)
            except Exception:
                pass

        visible_textareas = []
        for ta in textareas:
            try:
                if await ta.is_visible():
                    visible_textareas.append(ta)
            except Exception:
                pass

        # Un formulaire de contact a au minimum 2 champs + 1 textarea
        if len(visible_inputs) < 1 or len(visible_textareas) < 1:
            continue

        # Détecter les CAPTCHA
        has_captcha = bool(
            re.search(r"captcha|recaptcha|hcaptcha|g-recaptcha|turnstile", html_lower)
        )

        # Construire le sélecteur CSS du form
        form_id = await form.get_attribute("id")
        form_class = await form.get_attribute("class")
        if form_id:
            selector = f"form#{form_id}"
        elif form_class:
            first_class = form_class.strip().split()[0]
            selector = f"form.{first_class}"
        else:
            selector = "form"

        return FormDetectionResult(
            found=True,
            form_url=page.url,
            form_html=html[:5000],  # cap à 5KB pour la DB
            form_selector=selector,
            field_count=len(visible_inputs) + len(visible_textareas),
            has_textarea=len(visible_textareas) > 0,
            has_captcha=has_captcha,
        )

    return None


async def _navigate_to_contact(page: Page, base_url: str) -> bool:
    """Navigue vers la page contact. Retourne True si navigation réussie."""
    # Essayer les chemins classiques
    for path in CONTACT_PATHS:
        url = base_url.rstrip("/") + path
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=10_000)
            if resp and resp.ok:
                return True
        except Exception:
            continue

    # Chercher un lien "contact" dans la page d'accueil
    try:
        await page.goto(base_url, wait_until="domcontentloaded", timeout=10_000)
    except Exception:
        return False

    links = await page.locator("a").all()
    for link in links:
        try:
            text = (await link.text_content() or "").strip()
            href = await link.get_attribute("href") or ""
            if CONTACT_LINK_PATTERNS.search(text) or CONTACT_LINK_PATTERNS.search(href):
                await link.click(timeout=5000)
                await page.wait_for_load_state("domcontentloaded", timeout=10_000)
                return True
        except Exception:
            continue

    return False


async def detect_contact_form(page: Page, website: str) -> FormDetectionResult:
    """
    Détecte un formulaire de contact sur le site.
    Cherche d'abord sur la page d'accueil, puis navigue vers /contact.
    """
    base_url = website if website.startswith("http") else f"https://{website}"

    # 1. Essayer la page d'accueil
    try:
        await page.goto(base_url, wait_until="domcontentloaded", timeout=15_000)
    except Exception:
        return FormDetectionResult()

    result = await _find_form_on_page(page)
    if result:
        return result

    # 2. Naviguer vers la page contact
    if await _navigate_to_contact(page, base_url):
        result = await _find_form_on_page(page)
        if result:
            return result

    return FormDetectionResult()
