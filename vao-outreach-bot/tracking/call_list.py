"""
Génération de la liste d'appels du jour.
Formate les prospects Tier 1 à appeler avec contexte.
"""

from __future__ import annotations

import csv
import io
import datetime
import logging

from rich.console import Console
from rich.table import Table

from db.client import get_call_list, get_last_submission

log = logging.getLogger("vao.call_list")


def _format_activities(activities: list | None) -> str:
    """Formatte les types d'activité pour l'affichage."""
    if not activities:
        return "-"
    labels = {
        "creation_jardin": "Création jardins",
        "amenagement": "Aménagement",
        "entretien": "Entretien",
        "elagage": "Élagage",
        "terrasse": "Terrasse",
        "piscine": "Piscine",
        "cloture": "Clôture",
        "arrosage": "Arrosage",
    }
    return ", ".join(labels.get(a, a) for a in activities[:3])


def generate_call_list() -> list[dict]:
    """
    Récupère la liste d'appels du jour avec contexte.
    Retourne une liste enrichie avec le dernier message envoyé.
    """
    prospects = get_call_list()
    enriched = []

    for p in prospects:
        last_sub = get_last_submission(p["id"])
        entry = {
            "id": p["id"],
            "prenom": p.get("prenom_gerant") or "",
            "nom": p.get("nom_gerant") or "",
            "entreprise": p.get("company_name") or "",
            "telephone": p.get("phone") or "",
            "ville": p.get("city") or "",
            "score": p.get("outreach_score") or 0,
            "activites": _format_activities(p.get("activity_types")),
            "dernier_message": "",
            "dernier_envoi": "",
        }
        if last_sub:
            entry["dernier_message"] = (last_sub.get("message_sent") or "")[:200]
            sent_at = last_sub.get("completed_at") or ""
            if sent_at:
                entry["dernier_envoi"] = sent_at[:10]

        enriched.append(entry)

    return enriched


def print_call_list(prospects: list[dict] | None = None) -> None:
    """Affiche la liste d'appels dans le terminal avec Rich."""
    if prospects is None:
        prospects = generate_call_list()

    console = Console()

    if not prospects:
        console.print("[yellow]Aucun appel prévu aujourd'hui.[/yellow]")
        return

    table = Table(
        title=f"Appels du {datetime.date.today().isoformat()} — {len(prospects)} prospect(s)",
        show_lines=True,
    )
    table.add_column("#", style="dim", width=3)
    table.add_column("Prénom / Nom", min_width=18)
    table.add_column("Entreprise", min_width=20)
    table.add_column("Téléphone", min_width=14)
    table.add_column("Ville", min_width=12)
    table.add_column("Score", justify="right", width=6)
    table.add_column("Activités", min_width=20)
    table.add_column("Dernier msg (extrait)", min_width=30)

    for i, p in enumerate(prospects, 1):
        table.add_row(
            str(i),
            f"{p['prenom']} {p['nom']}".strip(),
            p["entreprise"],
            p["telephone"],
            p["ville"],
            f"{p['score']:.1f}",
            p["activites"],
            p["dernier_message"][:80] + ("…" if len(p["dernier_message"]) > 80 else ""),
        )

    console.print(table)


def export_csv(prospects: list[dict] | None = None) -> str:
    """Exporte la liste d'appels en CSV (retourne le contenu string)."""
    if prospects is None:
        prospects = generate_call_list()

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=[
        "prenom", "nom", "entreprise", "telephone", "ville",
        "score", "activites", "dernier_message", "dernier_envoi",
    ])
    writer.writeheader()
    for p in prospects:
        writer.writerow({k: p.get(k, "") for k in writer.fieldnames})

    return output.getvalue()
