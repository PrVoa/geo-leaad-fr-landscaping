"""
Nettoyage de la table landscapers — classification en 3 niveaux.

NIVEAU 1 — GARDER : nom contient un mot paysagiste positif → statut = 'nouveau'
NIVEAU 2 — EXCLURE : nom contient un mot/pattern d'exclusion fort → statut = 'exclu'
NIVEAU 3 — AMBIGU : vérification via API gouvernementale (code NAF)

Ne supprime jamais — change uniquement le statut.
Seuls les leads en statut 'nouveau' ou NULL sont traités.

Usage :
    python clean_leads.py              # analyse + mise à jour
    python clean_leads.py --dry-run    # affiche sans modifier
    python clean_leads.py --dept 06    # un département seulement
"""
import argparse
import asyncio
import logging
import re
import sys
import time
import unicodedata
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import asyncpg
import httpx
from dotenv import load_dotenv
import os

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

DATABASE_URL = os.getenv("DATABASE_URL", "")
if not DATABASE_URL:
    sys.exit("DATABASE_URL manquant dans .env")

_DB_URL = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://").replace(
    "postgres+asyncpg://", "postgresql://"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("clean_leads")

# ---------------------------------------------------------------------------
# NIVEAU 1 — Mots paysagistes positifs (garder directement)
# ---------------------------------------------------------------------------

MOTS_GARDER = [
    "paysage", "paysagiste", "paysagisme",
    "jardin", "jardins", "jardinier", "jardinage", "jardinage",
    "espaces verts", "espace vert",
    "elagage", "elagueur",
    "arboriste", "arborist", "arboriculture",
    "verdure", "vegetal",
    "bocage",
    "parcs et jardins", "parc et jardin",
    "esprit vert", "nature verte",
    "cimes",
    "horticulture", "horticole",
    "pepiniere",
    "amenagement paysager",
    "entretien jardin", "creation jardin",
]

# Mots positifs conditionnels : valides seulement si combinés avec un autre mot positif
MOTS_GARDER_CONDITIONNELS = {
    "abattage": ["elagage", "jardin", "paysage", "arbo", "arbres"],
}

# ---------------------------------------------------------------------------
# NIVEAU 2 — Mots d'exclusion forts (exclure directement)
# ---------------------------------------------------------------------------

# Marques/franchises hors-cible
FRANCHISES = [
    "azae", "domaliance", "maison et services", "apef ",
    "axeo services", "free dom", "centre services",
    "home services", "generale des services", "vivaservices",
    "domicile clean", "tout a dom", "bien dans sa maison",
    "daniel moquet", "idverde", "id verde",
    "serpe -", "terideal", "o2 jardinage", "o2 jardi",
]

# Formation
FORMATION = [
    "lycee", "cfa ", "cfppa", "mfr ", "ensp",
    "ecole nationale", "campus", "agrocampus",
]

# Associations / ESAT
ASSOCIATIONS = [
    "esat ", "association solidarite", "association intermediaire",
    "association internationale", "adapei", "adcr", "ladapt",
]

# Nettoyage pur (sans mot jardin — géré dans la logique)
NETTOYAGE = [
    "nettoyage professionnel", "entreprise de nettoyage",
    "clinitex", "proprete", "vitrerie",
]

# BTP / toiture pur (sans mot jardin — géré dans la logique)
BTP = [
    "couvreur", "toiture", "maconnerie sarl",
    "renovation construction",
]

# Intérim / RH
INTERIM = [
    "interim", "recrutement", "aquila rh", "temporis ",
    "r.a.s interim", "agri-interim",
]

# Tourisme / loisirs
TOURISME = [
    "camping", "office de tourisme", "planetarium",
    "plage ", "dunes ", "aire de jeux", "cimetiere",
]

# Espagnol
ESPAGNOL = [
    "limpiezas", "arquitectos", "estudio de arquitectura",
    "servicios y limpiezas",
]

# Divers
DIVERS = [
    "caue ", "notaire", "piscines de france", "demenagement",
]

# Groupes avec contexte : ces mots excluent SAUF si un mot jardin est présent
EXCLUSION_SANS_JARDIN = NETTOYAGE + BTP

# Tous les autres excluent sans condition
EXCLUSION_FERME = FRANCHISES + FORMATION + ASSOCIATIONS + INTERIM + TOURISME + ESPAGNOL + DIVERS

# Mots qui "sauvent" un lead des exclusions conditionnelles
MOTS_SAUVETAGE = ["jardin", "jardins", "paysage", "paysagiste", "elagage", "espace vert"]

# ---------------------------------------------------------------------------
# Codes NAF acceptés (API niveau 3)
# ---------------------------------------------------------------------------

NAF_GARDER = ("8130", "0161")   # entretien espaces verts, soutien agriculture

API_ENTREPRISES = "https://recherche-entreprises.api.gouv.fr/search"

# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

def normaliser(texte: str) -> str:
    """Minuscules, sans accents, sans ponctuation superflue, espaces normalisés."""
    t = texte.lower().strip()
    t = unicodedata.normalize("NFD", t)
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def contient_un(texte: str, mots: list[str]) -> str | None:
    """Retourne le premier mot trouvé, ou None."""
    for m in mots:
        if m in texte:
            return m
    return None

# ---------------------------------------------------------------------------
# NIVEAU 1 & 2 — Classification locale
# ---------------------------------------------------------------------------

def classifier_local(name: str) -> tuple[str | None, str]:
    """
    Retourne (decision, raison).
    decision : 'garder' | 'exclu' | None (ambigu → niveau 3)
    """
    n = normaliser(name)

    # ── Niveau 1 : mots paysagistes directs ──
    mot = contient_un(n, MOTS_GARDER)
    if mot:
        return "garder", f"mot positif: '{mot}'"

    # Mots conditionnels (ex: abattage seul ne suffit pas)
    for mot_cond, mots_requis in MOTS_GARDER_CONDITIONNELS.items():
        if mot_cond in n and contient_un(n, mots_requis):
            return "garder", f"mot positif conditionnel: '{mot_cond}'"

    # ── Niveau 2 : exclusions fermes (sans condition) ──
    mot = contient_un(n, EXCLUSION_FERME)
    if mot:
        return "exclu", f"exclusion ferme: '{mot}'"

    # ── Niveau 2 : exclusions conditionnelles (sauf si mot jardin présent) ──
    mot_excl = contient_un(n, EXCLUSION_SANS_JARDIN)
    if mot_excl:
        mot_salut = contient_un(n, MOTS_SAUVETAGE)
        if mot_salut:
            # Le mot jardin rachète l'exclusion → garder
            return "garder", f"mot positif '{mot_salut}' rachète exclusion '{mot_excl}'"
        return "exclu", f"exclusion conditionnelle: '{mot_excl}'"

    # ── Niveau 3 : ambigu ──
    return None, "ambigu"

# ---------------------------------------------------------------------------
# NIVEAU 3 — Vérification API gouvernementale
# ---------------------------------------------------------------------------

async def tester_api(timeout: float = 5.0) -> bool:
    """Retourne True si l'API gouvernementale est joignable."""
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                API_ENTREPRISES,
                params={"q": "test", "limit": 1},
                timeout=timeout,
            )
            return r.status_code < 500
    except Exception:
        return False


async def verifier_api(client: httpx.AsyncClient, name: str) -> tuple[str, str]:
    """
    Retourne (decision, raison).
    decision : 'garder' | 'exclu' | 'doute'
    """
    try:
        resp = await client.get(
            API_ENTREPRISES,
            params={"q": name, "limit": 1},
            timeout=8.0,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        return "doute", f"API erreur: {exc}"

    results = data.get("results", [])
    if not results:
        return "doute", "API: aucun résultat → bénéfice du doute"

    entreprise = results[0]

    # Vérifier si radiée / fermée
    etat = (entreprise.get("etat_administratif") or "").upper()
    if etat == "F":
        return "exclu", "API: entreprise radiée/fermée"

    # Chercher le code NAF dans les établissements
    activite_principale = (
        entreprise.get("activite_principale") or
        entreprise.get("code_naf") or
        ""
    )

    # Parfois dans siege ou matching_etablissements
    siege = entreprise.get("siege") or {}
    if not activite_principale:
        activite_principale = siege.get("activite_principale") or ""

    # Matching établissements
    matchs = entreprise.get("matching_etablissements") or []
    if not activite_principale and matchs:
        activite_principale = matchs[0].get("activite_principale") or ""

    naf = re.sub(r"[^0-9A-Za-z]", "", activite_principale).upper()

    if any(naf.startswith(code.replace(".", "")) for code in NAF_GARDER):
        return "garder", f"API: NAF {naf} → espaces verts/agriculture"

    if not naf:
        return "doute", "API: code NAF absent → bénéfice du doute"

    return "exclu", f"API: NAF {naf} → hors cible"

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run(dept_filter: str | None, dry_run: bool):
    # ── Test de connectivité API au démarrage ──
    api_disponible = await tester_api(timeout=5.0)
    if not api_disponible:
        log.warning("⚠️  API gouvernementale inaccessible - mode mots-clés uniquement (niveaux 1 et 2)")

    conn = await asyncpg.connect(_DB_URL)

    where_clauses = ["(statut = 'nouveau' OR statut IS NULL)"]
    if dept_filter:
        where_clauses.append(f"dept = '{dept_filter}'")
    where = " AND ".join(where_clauses)

    rows = await conn.fetch(
        f"""SELECT place_id, name, statut
            FROM landscapers
            WHERE {where}
            ORDER BY name"""
    )

    total = len(rows)
    prefix = "[DRY-RUN] " if dry_run else ""
    print(f"\n{prefix}Analyse de {total} leads...\n")

    a_garder:  dict[str, str] = {}   # place_id → raison
    a_exclure: dict[str, str] = {}   # place_id → raison
    a_doute:   dict[str, str] = {}   # place_id → raison

    # ── Étapes 1 & 2 : classification locale ──
    ambigus = []
    for row in rows:
        name = row["name"] or ""
        decision, raison = classifier_local(name)
        if decision == "garder":
            a_garder[row["place_id"]] = raison
            log.info("GARDER  %-55s  %s", name[:55], raison)
        elif decision == "exclu":
            a_exclure[row["place_id"]] = raison
            log.info("EXCLU   %-55s  %s", name[:55], raison)
        else:
            ambigus.append(dict(row))

    if api_disponible:
        print(f"  Après niveaux 1&2 : {len(a_garder)} gardés / {len(a_exclure)} exclus / {len(ambigus)} ambigus → API")
    else:
        print(f"  Après niveaux 1&2 : {len(a_garder)} gardés / {len(a_exclure)} exclus / {len(ambigus)} ambigus laissés 'nouveau' (API indisponible)")

    # ── Étape 3 : vérification API par batch de 100 ──
    nb_api_garder = 0
    nb_api_exclu  = 0
    nb_api_doute  = 0

    if api_disponible:
        async with httpx.AsyncClient() as client:
            for i in range(0, len(ambigus), 100):
                batch = ambigus[i:i + 100]
                print(f"\n  Batch API {i + 1}–{i + len(batch)} / {len(ambigus)}...")
                for row in batch:
                    name = row["name"] or ""
                    decision, raison = await verifier_api(client, name)
                    if decision == "garder":
                        a_garder[row["place_id"]] = raison
                        nb_api_garder += 1
                        log.info("API→GARDER  %-50s  %s", name[:50], raison)
                    elif decision == "exclu":
                        a_exclure[row["place_id"]] = raison
                        nb_api_exclu += 1
                        log.info("API→EXCLU   %-50s  %s", name[:50], raison)
                    else:
                        a_doute[row["place_id"]] = raison
                        nb_api_doute += 1
                        log.info("API→DOUTE   %-50s  %s", name[:50], raison)
                    await asyncio.sleep(0.5)

    # ── Résumé ──
    print(f"\n{'─' * 60}")
    print(f"  Total analysé    : {total}")
    print(f"  Gardés (nv 1&2)  : {len(a_garder) - nb_api_garder}")
    print(f"  Exclus (nv 1&2)  : {len(a_exclure) - nb_api_exclu}")
    if api_disponible:
        print(f"  Vérifiés API     : {len(ambigus)}")
        print(f"    └ gardés        : {nb_api_garder}")
        print(f"    └ exclus        : {nb_api_exclu}")
        print(f"    └ douteux       : {nb_api_doute}  (laissés 'nouveau')")
    else:
        print(f"  Ambigus (nv 3)   : {len(ambigus)}  (API indisponible → laissés 'nouveau')")
    print(f"  Total gardés     : {len(a_garder) + nb_api_doute + (len(ambigus) if not api_disponible else 0)}")
    print(f"  Total exclus     : {len(a_exclure)}")
    print(f"{'─' * 60}\n")

    # Aperçu exclusions
    if a_exclure:
        print("Aperçu des 20 premières exclusions :")
        for pid, raison in list(a_exclure.items())[:20]:
            row = next((r for r in rows if r["place_id"] == pid), None)
            nom = (row["name"] if row else pid) or pid
            print(f"  - {nom[:52]:<52}  [{raison}]")
        if len(a_exclure) > 20:
            print(f"  ... et {len(a_exclure) - 20} autres")

    # ── Mise à jour base ──
    if dry_run:
        print("\n[DRY-RUN] Aucune modification effectuée.")
        await conn.close()
        return

    if a_exclure:
        ids_exclu = list(a_exclure.keys())
        for i in range(0, len(ids_exclu), 500):
            batch = ids_exclu[i:i + 500]
            await conn.execute(
                "UPDATE landscapers SET statut = 'exclu' WHERE place_id = ANY($1::text[])",
                batch,
            )
        print(f"{len(a_exclure)} leads mis à jour → statut='exclu'")
    else:
        print("Aucun lead à exclure.")

    await conn.close()


def main():
    parser = argparse.ArgumentParser(description="Nettoyage des leads paysagistes (3 niveaux)")
    parser.add_argument("--dept",    metavar="XX", help="Limite à un département (ex: 06)")
    parser.add_argument("--dry-run", action="store_true", help="Analyse sans modifier la base")
    args = parser.parse_args()

    asyncio.run(run(args.dept, args.dry_run))


if __name__ == "__main__":
    main()
