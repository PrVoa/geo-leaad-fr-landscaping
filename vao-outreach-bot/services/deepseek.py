"""Client DeepSeek API — fallback LLM pour formulaires ambigus."""

from __future__ import annotations

import json
import logging

from openai import OpenAI

from config.settings import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL, has_deepseek

log = logging.getLogger("vao.deepseek")

_client: OpenAI | None = None

FORM_MAPPING_SYSTEM = """Tu es un expert en analyse de formulaires HTML.
Analyse le formulaire HTML suivant et retourne un JSON avec le mapping des champs.

Pour chaque champ du formulaire (<input>, <textarea>, <select>), identifie sa fonction parmi :
- "name" : champ nom/prénom
- "email" : champ email
- "phone" : champ téléphone
- "subject" : champ sujet/objet
- "message" : champ message principal (textarea)
- "company" : champ nom d'entreprise
- "honeypot" : champ caché (ne PAS remplir)
- "other" : champ non pertinent

Retourne UNIQUEMENT un JSON valide, sans aucun texte avant ou après :
{
  "fields": [
    {"selector": "CSS selector du champ", "role": "name|email|phone|subject|message|company|honeypot|other", "required": true|false}
  ],
  "submit_selector": "CSS selector du bouton submit",
  "form_selector": "CSS selector du form",
  "confidence": 0.0-1.0
}"""


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        if not has_deepseek():
            raise RuntimeError("DEEPSEEK_API_KEY non configuré")
        _client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)
    return _client


def analyze_form_html(form_html: str) -> dict | None:
    """
    Envoie le HTML d'un formulaire à DeepSeek pour analyse.
    Retourne le mapping dict ou None en cas d'erreur.
    """
    if not has_deepseek():
        log.debug("DeepSeek non configuré, skip LLM")
        return None

    try:
        # Tronquer le HTML si trop long
        if len(form_html) > 8000:
            form_html = form_html[:8000] + "\n<!-- tronqué -->"

        response = _get_client().chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=[
                {"role": "system", "content": FORM_MAPPING_SYSTEM},
                {"role": "user", "content": form_html},
            ],
            temperature=0.1,
            max_tokens=1000,
        )

        content = response.choices[0].message.content or ""
        # Extraire le JSON du contenu
        content = content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1]
            content = content.rsplit("```", 1)[0]
        content = content.strip()

        result = json.loads(content)
        log.info("DeepSeek mapping OK, confidence: %s", result.get("confidence"))
        return result

    except json.JSONDecodeError as e:
        log.warning("DeepSeek retour JSON invalide: %s", e)
        return None
    except Exception as e:
        log.warning("DeepSeek erreur: %s", e)
        return None
