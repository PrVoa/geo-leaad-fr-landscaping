"""
Génération de messages personnalisés — méthode Cyrano APPC.
Charge les templates, remplace les variables, valide les contraintes.
"""

from __future__ import annotations

import re
import logging
from pathlib import Path
from dataclasses import dataclass

from config.settings import TEMPLATES_DIR, SENDER_PHONE, SENDER_NAME
from config.sequences import get_step

log = logging.getLogger("vao.message_builder")

# Mapping activité → texte lisible
ACTIVITY_LABELS = {
    "creation_jardin": "création de jardins",
    "amenagement": "aménagement extérieur",
    "entretien": "entretien d'espaces verts",
    "elagage": "élagage",
    "terrasse": "terrasse",
    "piscine": "piscine",
    "cloture": "clôture",
    "arrosage": "arrosage automatique",
    "architecte": "architecture paysagère",
    "tonte": "tonte et entretien",
}

# Régions par département (principales)
DEPT_TO_REGION = {
    "01": "Ain", "02": "Aisne", "03": "Allier", "04": "Alpes-de-Haute-Provence",
    "05": "Hautes-Alpes", "06": "Alpes-Maritimes", "07": "Ardèche", "08": "Ardennes",
    "09": "Ariège", "10": "Aube", "11": "Aude", "12": "Aveyron",
    "13": "Bouches-du-Rhône", "14": "Calvados", "15": "Cantal", "16": "Charente",
    "17": "Charente-Maritime", "18": "Cher", "19": "Corrèze",
    "21": "Côte-d'Or", "22": "Côtes-d'Armor", "23": "Creuse", "24": "Dordogne",
    "25": "Doubs", "26": "Drôme", "27": "Eure", "28": "Eure-et-Loir",
    "29": "Finistère", "30": "Gard", "31": "Haute-Garonne", "32": "Gers",
    "33": "Gironde", "34": "Hérault", "35": "Ille-et-Vilaine", "36": "Indre",
    "37": "Indre-et-Loire", "38": "Isère", "39": "Jura", "40": "Landes",
    "41": "Loir-et-Cher", "42": "Loire", "43": "Haute-Loire", "44": "Loire-Atlantique",
    "45": "Loiret", "46": "Lot", "47": "Lot-et-Garonne", "48": "Lozère",
    "49": "Maine-et-Loire", "50": "Manche", "51": "Marne", "52": "Haute-Marne",
    "53": "Mayenne", "54": "Meurthe-et-Moselle", "55": "Meuse", "56": "Morbihan",
    "57": "Moselle", "58": "Nièvre", "59": "Nord", "60": "Oise",
    "61": "Orne", "62": "Pas-de-Calais", "63": "Puy-de-Dôme", "64": "Pyrénées-Atlantiques",
    "65": "Hautes-Pyrénées", "66": "Pyrénées-Orientales", "67": "Bas-Rhin", "68": "Haut-Rhin",
    "69": "Rhône", "70": "Haute-Saône", "71": "Saône-et-Loire", "72": "Sarthe",
    "73": "Savoie", "74": "Haute-Savoie", "75": "Paris", "76": "Seine-Maritime",
    "77": "Seine-et-Marne", "78": "Yvelines", "79": "Deux-Sèvres", "80": "Somme",
    "81": "Tarn", "82": "Tarn-et-Garonne", "83": "Var", "84": "Vaucluse",
    "85": "Vendée", "86": "Vienne", "87": "Haute-Vienne", "88": "Vosges",
    "89": "Yonne", "90": "Territoire de Belfort",
    "91": "Essonne", "92": "Hauts-de-Seine", "93": "Seine-Saint-Denis",
    "94": "Val-de-Marne", "95": "Val-d'Oise",
    "2A": "Corse-du-Sud", "2B": "Haute-Corse",
}


@dataclass
class BuiltMessage:
    body: str
    subject: str | None = None  # Seulement pour les steps email (4-5)
    variant: str = "A"
    step: int = 0
    word_count: int = 0
    warnings: list[str] | None = None


def _resolve_activity(prospect: dict) -> str:
    """Détermine l'activité principale du prospect."""
    activities = prospect.get("activity_types") or []
    if activities:
        # Prioriser les activités à forte marge
        priority = ["creation_jardin", "amenagement", "terrasse", "piscine",
                     "elagage", "entretien", "cloture", "arrosage", "architecte"]
        for act in priority:
            if act in activities:
                return ACTIVITY_LABELS.get(act, act)
        return ACTIVITY_LABELS.get(activities[0], activities[0])
    return "aménagement extérieur"  # fallback générique


def _resolve_region(prospect: dict) -> str:
    """Détermine la région/département du prospect."""
    region = prospect.get("region")
    if region:
        return region
    dept = prospect.get("dept") or ""
    return DEPT_TO_REGION.get(dept, dept or "votre région")


def _build_variables(prospect: dict) -> dict:
    """Construit le dict de variables pour le template."""
    prenom = prospect.get("prenom_gerant") or ""
    if not prenom:
        # Extraire du nom complet si possible
        nom_complet = prospect.get("nom_gerant") or ""
        parts = nom_complet.split()
        prenom = parts[0] if parts else ""

    return {
        "prenom": prenom or "Bonjour",
        "nom_entreprise": prospect.get("company_name") or "votre entreprise",
        "ville": prospect.get("city") or "votre ville",
        "department": prospect.get("dept") or "",
        "region": _resolve_region(prospect),
        "activite_principale": _resolve_activity(prospect),
        "site_web": prospect.get("website") or "",
        "phone": SENDER_PHONE,
    }


def _load_template(step: int, variant: str) -> str:
    """Charge un template depuis le dossier templates/."""
    step_config = get_step(step)
    if not step_config:
        raise ValueError(f"Step {step} inconnu")

    base_name = step_config["template"].replace(".txt", "")
    filename = f"{base_name}_{variant}.txt"
    path = TEMPLATES_DIR / filename

    if not path.exists():
        raise FileNotFoundError(f"Template non trouvé: {path}")

    return path.read_text(encoding="utf-8")


def _validate_message(body: str, step: int, channel: str) -> list[str]:
    """
    Checklist anti-erreurs Cyrano.
    Retourne une liste de warnings (vide = OK).
    """
    warnings = []
    body_lower = body.lower()

    # 1. Pas de prix/tarif
    if re.search(r"\d+\s*[€$]|\btarif\b|\bprix\b|\b\d+\s*euros?\b", body_lower):
        warnings.append("Contient un prix ou tarif")

    # 2. Pas de demande de RDV (steps 1-3)
    if step <= 3 and re.search(r"\brdv\b|\brendez.vous\b|\bcréneaux?\b|\bdisponible\b", body_lower):
        warnings.append(f"Step {step} demande un RDV (interdit steps 1-3)")

    # 3. Ne commence pas par parler de VAO
    first_lines = body[:200].lower()
    if re.search(r"^(nous sommes|on est|chez vao|vao est|je suis quentin)", first_lines):
        warnings.append("Le message commence par parler de l'expéditeur")

    # 4. Pas de small talk
    if "j'espère que vous allez bien" in body_lower or "j espère que" in body_lower:
        warnings.append("Small talk détecté")

    # 5. Max 1 lien
    urls = re.findall(r"https?://\S+", body)
    if len(urls) > 1:
        warnings.append(f"Trop de liens ({len(urls)}, max 1)")

    # 6. Pas de liste de features
    bullet_count = body.count("•") + body.count("- ") + body.count("✓")
    if bullet_count >= 3:
        warnings.append("Liste de features détectée")

    # 7. Personnalisation visible (au moins prenom ou nom_entreprise a été remplacé)
    if "{prenom}" in body or "{nom_entreprise}" in body:
        warnings.append("Variable non remplacée dans le message")

    # 8. Se termine par une question concrète
    lines = [l.strip() for l in body.strip().split("\n") if l.strip()]
    # Chercher la question dans les dernières lignes (avant la signature)
    content_lines = []
    for line in lines:
        if line.startswith("Quentin") or line.startswith(SENDER_PHONE) or re.match(r"^0\d[\s.]{0,2}\d{2}", line):
            break
        content_lines.append(line)
    if content_lines:
        last_content = content_lines[-1]
        if "?" not in last_content:
            # Tolérer le step 5 (breakup) qui peut ne pas finir par une question
            if step != 5:
                warnings.append("Ne se termine pas par une question")

    # 9. Word count
    word_count = len(body.split())
    max_words = 120 if channel == "email" else 90
    if word_count > max_words:
        warnings.append(f"Trop long ({word_count} mots, max {max_words})")

    return warnings


def build_message(prospect: dict, step: int, variant: str = "A") -> BuiltMessage:
    """
    Construit le message personnalisé pour un prospect à un step donné.
    Charge le template, remplace les variables, valide.
    """
    step_config = get_step(step)
    if not step_config:
        raise ValueError(f"Step {step} inconnu")

    channel = step_config["channel"]
    template = _load_template(step, variant)
    variables = _build_variables(prospect)

    # Remplacer les variables
    body = template
    for key, value in variables.items():
        body = body.replace(f"{{{key}}}", value)

    # Sujet email (steps 4-5)
    subject = None
    if channel == "email" and "email_subject" in step_config:
        subject = step_config["email_subject"]
        for key, value in variables.items():
            subject = subject.replace(f"{{{key}}}", value)

    # Validation Cyrano
    warnings = _validate_message(body, step, channel)
    if warnings:
        for w in warnings:
            log.warning("Step %d variant %s — %s", step, variant, w)

    return BuiltMessage(
        body=body,
        subject=subject,
        variant=variant,
        step=step,
        word_count=len(body.split()),
        warnings=warnings,
    )
