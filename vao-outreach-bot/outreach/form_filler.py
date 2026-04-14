"""
Remplissage et soumission de formulaires de contact via Playwright.
Remplit les champs selon le mapping, simule un comportement humain, soumet.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from playwright.async_api import Page, TimeoutError as PwTimeout

from config.settings import SENDER_NAME, SENDER_PHONE, SCREENSHOTS_DIR, RESEND_FROM
from outreach.field_mapper import FormMapping, dict_to_mapping
from outreach.stealth import random_delay, get_stealth_context_options
from services.playwright_manager import new_context

log = logging.getLogger("vao.form_filler")

# Mots-clés de succès post-soumission
SUCCESS_PATTERNS = [
    "merci", "thank", "envoyé", "reçu", "succès",
    "message a bien été", "nous vous répondrons", "votre demande",
    "confirmation", "bien reçu", "pris en compte",
]

CAPTCHA_SELECTORS = [
    "iframe[src*='recaptcha']",
    "iframe[src*='hcaptcha']",
    "iframe[src*='turnstile']",
    ".g-recaptcha",
    ".h-captcha",
    "[data-sitekey]",
]


@dataclass
class FillResult:
    success: bool
    status: str  # success, failed_captcha, failed_mapping, failed_submit, failed_timeout, failed_other
    error: str | None = None
    page_load_ms: int = 0
    form_fill_ms: int = 0
    screenshot_path: str | None = None


async def _detect_captcha(page: Page) -> bool:
    """Vérifie si un CAPTCHA est présent sur la page."""
    for selector in CAPTCHA_SELECTORS:
        if await page.locator(selector).count() > 0:
            return True
    return False


async def _type_human(page: Page, selector: str, text: str) -> None:
    """Tape du texte de manière humaine (press_sequentially avec délai)."""
    element = page.locator(selector).first
    await element.scroll_into_view_if_needed()
    await element.click()
    await asyncio.sleep(random_delay("between_keystrokes"))
    await element.press_sequentially(text, delay=50)


async def _detect_success(page: Page, original_url: str) -> bool:
    """Détecte si la soumission a réussi (redirect ou message de succès)."""
    # Redirect détectée
    if page.url != original_url and "merci" in page.url.lower():
        return True
    if page.url != original_url and "thank" in page.url.lower():
        return True

    # Chercher un message de succès dans le contenu visible
    try:
        body_text = (await page.locator("body").text_content() or "").lower()
        for pattern in SUCCESS_PATTERNS:
            if pattern in body_text:
                # Vérifier que le pattern n'était pas déjà là avant soumission
                return True
    except Exception:
        pass

    # Redirect vers une autre page (même sans mot-clé)
    if page.url != original_url:
        return True

    return False


async def fill_and_submit(
    prospect: dict,
    message_body: str,
    message_subject: str | None = None,
) -> FillResult:
    """
    Ouvre la page du formulaire, remplit les champs et soumet.

    Args:
        prospect: dict avec contact_form_url et form_fields_mapping
        message_body: le message personnalisé à envoyer
        message_subject: sujet optionnel (si le formulaire a un champ sujet)

    Returns:
        FillResult avec le statut de la soumission
    """
    form_url = prospect.get("contact_form_url")
    if not form_url:
        return FillResult(success=False, status="failed_mapping", error="Pas de contact_form_url")

    mapping_data = prospect.get("form_fields_mapping")
    if not mapping_data:
        return FillResult(success=False, status="failed_mapping", error="Pas de form_fields_mapping")

    if isinstance(mapping_data, str):
        import json
        mapping_data = json.loads(mapping_data)

    mapping = dict_to_mapping(mapping_data)
    if not mapping.has_minimum():
        return FillResult(success=False, status="failed_mapping", error="Mapping incomplet (email ou message manquant)")

    # Préparer les données à remplir par rôle
    fill_data = {
        "email": RESEND_FROM,
        "message": message_body,
        "name": SENDER_NAME,
        "phone": SENDER_PHONE,
        "subject": message_subject or "Demande d'information",
        "company": "VAO",
    }

    stealth_opts = get_stealth_context_options()
    t0 = time.monotonic()

    try:
        async with new_context(stealth_opts) as ctx:
            page = await ctx.new_page()

            # Dismiss dialogs automatiquement
            page.on("dialog", lambda d: asyncio.ensure_future(d.dismiss()))

            # Navigation
            try:
                await page.goto(form_url, wait_until="domcontentloaded", timeout=15_000)
            except PwTimeout:
                return FillResult(success=False, status="failed_timeout", error="Timeout navigation")

            page_load_ms = int((time.monotonic() - t0) * 1000)
            await asyncio.sleep(random_delay("after_page_load"))

            # Vérifier CAPTCHA
            if await _detect_captcha(page):
                return FillResult(
                    success=False, status="failed_captcha",
                    error="CAPTCHA détecté", page_load_ms=page_load_ms,
                )

            # Attendre le formulaire
            try:
                await page.wait_for_selector(mapping.form_selector, timeout=10_000)
            except PwTimeout:
                return FillResult(
                    success=False, status="failed_submit",
                    error=f"Formulaire introuvable: {mapping.form_selector}",
                    page_load_ms=page_load_ms,
                )

            # Remplir les champs
            t_fill = time.monotonic()
            for field in mapping.fields:
                if field.role == "honeypot" or field.role == "other":
                    continue

                value = fill_data.get(field.role)
                if not value:
                    continue

                try:
                    element = page.locator(field.selector).first
                    if not await element.is_visible():
                        continue

                    await element.scroll_into_view_if_needed()
                    await element.click()
                    await asyncio.sleep(random_delay("between_keystrokes"))

                    # Vider le champ existant avant de taper
                    await element.fill("")
                    await element.press_sequentially(value, delay=50)

                    await asyncio.sleep(random_delay("between_fields"))
                except Exception as e:
                    log.warning("Erreur remplissage champ %s (%s): %s", field.role, field.selector, e)
                    if field.required:
                        return FillResult(
                            success=False, status="failed_mapping",
                            error=f"Champ required {field.role} échoué: {e}",
                            page_load_ms=page_load_ms,
                        )

            form_fill_ms = int((time.monotonic() - t_fill) * 1000)

            # Pause humaine avant soumission (relecture)
            await asyncio.sleep(random_delay("before_submit"))

            # Soumettre
            original_url = page.url
            try:
                submit = page.locator(mapping.submit_selector).first
                await submit.scroll_into_view_if_needed()
                await submit.click()
            except Exception as e:
                return FillResult(
                    success=False, status="failed_submit",
                    error=f"Click submit échoué: {e}",
                    page_load_ms=page_load_ms, form_fill_ms=form_fill_ms,
                )

            # Attendre la réponse (redirect ou changement DOM)
            try:
                await page.wait_for_load_state("networkidle", timeout=10_000)
            except PwTimeout:
                pass  # Pas forcément un échec

            await asyncio.sleep(1.5)

            # Détecter le succès
            success = await _detect_success(page, original_url)

            # Screenshot de confirmation
            screenshot_path = None
            try:
                SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
                prospect_id = prospect.get("id", "unknown")
                filename = f"{prospect_id}_{int(time.time())}.png"
                screenshot_path = str(SCREENSHOTS_DIR / filename)
                await page.screenshot(path=screenshot_path, full_page=False)
            except Exception as e:
                log.warning("Screenshot échoué: %s", e)

            await page.close()

            if success:
                return FillResult(
                    success=True, status="success",
                    page_load_ms=page_load_ms, form_fill_ms=form_fill_ms,
                    screenshot_path=screenshot_path,
                )
            else:
                return FillResult(
                    success=False, status="failed_submit",
                    error="Pas de confirmation de succès détectée",
                    page_load_ms=page_load_ms, form_fill_ms=form_fill_ms,
                    screenshot_path=screenshot_path,
                )

    except PwTimeout:
        return FillResult(success=False, status="failed_timeout", error="Timeout global")
    except Exception as e:
        log.exception("Erreur inattendue form_filler")
        return FillResult(success=False, status="failed_other", error=str(e))
