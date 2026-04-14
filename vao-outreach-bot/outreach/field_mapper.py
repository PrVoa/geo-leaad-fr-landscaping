"""
Mapping des champs de formulaire : heuristique d'abord, DeepSeek en fallback.
Identifie nom, email, phone, subject, message, honeypot dans un formulaire HTML.
"""

from __future__ import annotations

import re
import json
import logging
from dataclasses import dataclass, field

from playwright.async_api import Page, Locator

log = logging.getLogger("vao.field_mapper")


@dataclass
class FieldMapping:
    selector: str
    role: str  # name, email, phone, subject, message, company, honeypot, other
    required: bool = False
    confidence: float = 1.0


@dataclass
class FormMapping:
    fields: list[FieldMapping] = field(default_factory=list)
    submit_selector: str = ""
    form_selector: str = ""
    confidence: float = 0.0
    used_llm: bool = False

    def get(self, role: str) -> FieldMapping | None:
        for f in self.fields:
            if f.role == role:
                return f
        return None

    def has_minimum(self) -> bool:
        """Vérifie qu'on a au moins email + message."""
        roles = {f.role for f in self.fields}
        return "email" in roles and "message" in roles


# ── Patterns heuristiques ──────────────────────────────────────────────────

ROLE_PATTERNS: dict[str, dict] = {
    "email": {
        "input_types": ["email"],
        "name_re": re.compile(r"e?mail|courriel", re.I),
        "placeholder_re": re.compile(r"e?mail|votre\s+e?mail|adresse", re.I),
        "label_re": re.compile(r"e[\-\s]?mail|courriel", re.I),
    },
    "phone": {
        "input_types": ["tel"],
        "name_re": re.compile(r"t[eé]l[eé]?|phone|mobile|portable", re.I),
        "placeholder_re": re.compile(r"t[eé]l[eé]?phone|phone|06|07|portable", re.I),
        "label_re": re.compile(r"t[eé]l[eé]?phone|phone|portable", re.I),
    },
    "name": {
        "input_types": ["text"],
        "name_re": re.compile(r"^(nom|name|prenom|first.?name|last.?name|your.?name|full.?name)$", re.I),
        "placeholder_re": re.compile(r"(votre\s+)?nom|pr[eé]nom|name", re.I),
        "label_re": re.compile(r"nom|pr[eé]nom|name", re.I),
    },
    "subject": {
        "input_types": ["text"],
        "name_re": re.compile(r"subj|sujet|objet|your.?subject", re.I),
        "placeholder_re": re.compile(r"sujet|objet|subject", re.I),
        "label_re": re.compile(r"sujet|objet|subject", re.I),
    },
    "company": {
        "input_types": ["text"],
        "name_re": re.compile(r"(soci[eé]t[eé]|company|entreprise|raison.?sociale)", re.I),
        "placeholder_re": re.compile(r"soci[eé]t[eé]|entreprise|company", re.I),
        "label_re": re.compile(r"soci[eé]t[eé]|entreprise|company", re.I),
    },
}

# Signaux de honeypot
HONEYPOT_NAME_RE = re.compile(r"honeypot|hp_|_hp|fax|website|url|address2", re.I)


async def _is_honeypot(element: Locator) -> bool:
    """Détecte si un champ est un honeypot (ne pas remplir)."""
    try:
        # Nom suspect
        name = await element.get_attribute("name") or ""
        if HONEYPOT_NAME_RE.search(name):
            return True

        # type=hidden
        input_type = (await element.get_attribute("type") or "").lower()
        if input_type == "hidden":
            return True

        # aria-hidden
        if await element.get_attribute("aria-hidden") == "true":
            return True

        # tabindex=-1 (souvent honeypot)
        tabindex = await element.get_attribute("tabindex")
        if tabindex == "-1":
            # Vérifier aussi si caché visuellement
            bbox = await element.bounding_box()
            if bbox and (bbox["width"] == 0 or bbox["height"] == 0):
                return True

        # Vérifier la visibilité réelle
        is_visible = await element.is_visible()
        if not is_visible:
            return True

    except Exception:
        pass

    return False


async def _find_label_text(page: Page, element: Locator) -> str:
    """Trouve le texte du label associé à un champ."""
    try:
        el_id = await element.get_attribute("id")
        if el_id:
            label = page.locator(f"label[for='{el_id}']")
            if await label.count() > 0:
                return (await label.first.text_content() or "").strip()

        # Label parent
        parent_label = element.locator("xpath=ancestor::label")
        if await parent_label.count() > 0:
            return (await parent_label.first.text_content() or "").strip()
    except Exception:
        pass
    return ""


async def _classify_field(page: Page, element: Locator, tag: str) -> FieldMapping | None:
    """Classifie un champ de formulaire par heuristique."""
    if await _is_honeypot(element):
        selector = await _build_selector(element)
        return FieldMapping(selector=selector, role="honeypot")

    # Textarea → message
    if tag == "textarea":
        selector = await _build_selector(element)
        required = await element.get_attribute("required") is not None
        return FieldMapping(selector=selector, role="message", required=required)

    input_type = (await element.get_attribute("type") or "text").lower()
    name = (await element.get_attribute("name") or "").lower()
    placeholder = (await element.get_attribute("placeholder") or "").lower()
    label_text = (await _find_label_text(page, element)).lower()

    # Matcher les rôles
    best_role = None
    best_score = 0

    for role, patterns in ROLE_PATTERNS.items():
        score = 0

        # Match par type d'input
        if input_type in patterns.get("input_types", []):
            score += 3

        # Match par attribut name
        if patterns["name_re"].search(name):
            score += 2

        # Match par placeholder
        if patterns["placeholder_re"].search(placeholder):
            score += 2

        # Match par label
        if patterns["label_re"].search(label_text):
            score += 2

        if score > best_score:
            best_score = score
            best_role = role

    if not best_role or best_score < 2:
        return None  # Champ non reconnu, on ignore

    selector = await _build_selector(element)
    required = await element.get_attribute("required") is not None
    confidence = min(best_score / 7.0, 1.0)

    return FieldMapping(
        selector=selector, role=best_role, required=required, confidence=confidence
    )


async def _build_selector(element: Locator) -> str:
    """Construit un sélecteur CSS stable pour un élément."""
    el_id = await element.get_attribute("id")
    if el_id:
        tag = await element.evaluate("el => el.tagName.toLowerCase()")
        return f"{tag}#{el_id}"

    name = await element.get_attribute("name")
    if name:
        tag = await element.evaluate("el => el.tagName.toLowerCase()")
        return f'{tag}[name="{name}"]'

    # Fallback : type + nth-of-type
    tag = await element.evaluate("el => el.tagName.toLowerCase()")
    input_type = await element.get_attribute("type")
    if input_type:
        return f'{tag}[type="{input_type}"]'
    return tag


async def _find_submit(page: Page, form_selector: str) -> str:
    """Trouve le sélecteur du bouton submit."""
    form = page.locator(form_selector)

    # bouton type=submit
    submit_btn = form.locator('button[type="submit"], input[type="submit"]')
    if await submit_btn.count() > 0:
        el_id = await submit_btn.first.get_attribute("id")
        if el_id:
            return f"#{el_id}"
        return f'{form_selector} button[type="submit"], {form_selector} input[type="submit"]'

    # Bouton sans type explicite (dans un form, c'est submit par défaut)
    btn = form.locator("button")
    if await btn.count() > 0:
        return f"{form_selector} button"

    return f"{form_selector} [type='submit']"


async def map_form_fields(page: Page, form_selector: str) -> FormMapping:
    """
    Analyse un formulaire et mappe chaque champ à son rôle.
    Approche heuristique pure — couvre ~75% des cas.
    """
    form = page.locator(form_selector)
    if await form.count() == 0:
        return FormMapping()

    # Collecter tous les inputs visibles et textareas
    inputs = await form.locator("input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=checkbox]):not([type=radio])").all()
    textareas = await form.locator("textarea").all()

    fields: list[FieldMapping] = []

    for el in inputs:
        mapping = await _classify_field(page, el, "input")
        if mapping:
            fields.append(mapping)

    for el in textareas:
        mapping = await _classify_field(page, el, "textarea")
        if mapping:
            fields.append(mapping)

    # Trouver le bouton submit
    submit_selector = await _find_submit(page, form_selector)

    # Calculer la confiance globale
    roles = {f.role for f in fields if f.role != "honeypot"}
    has_email = "email" in roles
    has_message = "message" in roles
    confidence = sum(f.confidence for f in fields if f.role != "honeypot") / max(len(fields), 1)

    if has_email and has_message:
        confidence = max(confidence, 0.7)

    return FormMapping(
        fields=fields,
        submit_selector=submit_selector,
        form_selector=form_selector,
        confidence=confidence,
    )


def mapping_to_dict(mapping: FormMapping) -> dict:
    """Sérialise un FormMapping pour stockage en JSONB."""
    return {
        "fields": [
            {"selector": f.selector, "role": f.role, "required": f.required}
            for f in mapping.fields
        ],
        "submit_selector": mapping.submit_selector,
        "form_selector": mapping.form_selector,
        "confidence": mapping.confidence,
        "used_llm": mapping.used_llm,
    }


def dict_to_mapping(data: dict) -> FormMapping:
    """Désérialise un FormMapping depuis un dict JSONB."""
    return FormMapping(
        fields=[
            FieldMapping(selector=f["selector"], role=f["role"], required=f.get("required", False))
            for f in data.get("fields", [])
        ],
        submit_selector=data.get("submit_selector", ""),
        form_selector=data.get("form_selector", ""),
        confidence=data.get("confidence", 0.0),
        used_llm=data.get("used_llm", False),
    )
