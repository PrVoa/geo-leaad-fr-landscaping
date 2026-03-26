"""
Nettoyage de la table landscapers.

Règles :
  1. Mots-clés "garder"  → statut inchangé (lead valide)
  2. Mots-clés "exclure" (sans mot garder) → statut = 'exclu'
  3. Franchises hors-cible → toujours exclu
  4. Doublons (noms quasi-identiques) → garder le plus complet, exclure l'autre

Ne supprime jamais — change uniquement le statut.
Seuls les leads en statut 'nouveau' sont traités automatiquement
(les leads déjà contacté/intéressé/client/perdu ne sont pas touchés).

Usage :
    python clean_leads.py              # analyse + mise à jour
    python clean_leads.py --dry-run    # affiche sans modifier
    python clean_leads.py --dept 06    # un département seulement
"""
import argparse
import asyncio
import re
import sys
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import asyncpg
from dotenv import load_dotenv
import os

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

DATABASE_URL = os.getenv("DATABASE_URL", "")
if not DATABASE_URL:
    sys.exit("DATABASE_URL manquant dans .env")

_DB_URL = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://").replace(
    "postgres+asyncpg://", "postgresql://"
)

# ---------------------------------------------------------------------------
# Règles métier
# ---------------------------------------------------------------------------

MOTS_GARDER = [
    "paysage", "paysagiste", "espaces verts", "espace vert",
    "jardin", "jardins", "jardinier", "jardiniere",
    "elagage", "elagueur", "arboriste", "arboriculture",
    "entretien vert", "nature", "verdure", "parc", "bocage",
    "cimes", "arbres", "taille", "haie", "gazon", "pelouse",
    "plantation", "engazonnement", "paysagisme",
]

MOTS_EXCLURE = [
    "association", "esat", "lycee", "camping", "office de tourisme",
    "plage", "dune", "aire de jeux", "cimetiere", "planetarium",
    "nettoyage", "menage", "repassage", "bricolage", "peinture",
    "maconnerie", "beton", "interim", "agence", "notaire",
    "formation", "cfa", "caue", "services a domicile",
    "multiservices", "manutention", "demenagement", "electricite",
    "plomberie", "chauffage", "carrelage", "toiture", "couverture",
    "agriculture", "viticulture", "exploitation agricole",
    "pompes funebres", "funeraire",
]

# Franchises toujours hors-cible (même si "jardinage" dans le nom)
FRANCHISES_EXCLURE = [
    "eurovia", "daniel moquet", "maison et services",
    "centre services", "axeo services", "shiva", "o2 care",
    "les 3 menages",
]

# Cas spécial : multiservices n'est exclu QUE s'il n'y a pas de mot garder
# (déjà géré par la logique principale)


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

def normaliser(texte: str) -> str:
    """Minuscules, sans accents, sans ponctuation, espaces normalisés."""
    t = texte.lower().strip()
    t = unicodedata.normalize("NFD", t)
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")  # retire accents
    t = re.sub(r"[^\w\s]", " ", t)   # ponctuation → espace
    t = re.sub(r"\s+", " ", t).strip()
    return t


def contient(texte_norme: str, mots: list[str]) -> bool:
    return any(m in texte_norme for m in mots)


# ---------------------------------------------------------------------------
# Classification d'un lead
# ---------------------------------------------------------------------------

def classifier(name: str, categorie: str | None) -> str | None:
    """
    Retourne 'exclu' si le lead est hors-cible, None si lead valide.
    Analyse le nom ET la catégorie Google Maps.
    """
    champs = " ".join(filter(None, [name, categorie]))
    n = normaliser(champs)

    # 1. Franchise → toujours exclu
    if contient(n, FRANCHISES_EXCLURE):
        return "exclu"

    # 2. Contient un mot "garder" → valide quoi qu'il arrive
    if contient(n, MOTS_GARDER):
        return None

    # 3. Contient un mot "exclure" → exclu
    if contient(n, MOTS_EXCLURE):
        return "exclu"

    # 4. Pas de mot clé ni dans un sens ni dans l'autre → on ne touche pas
    return None


# ---------------------------------------------------------------------------
# Détection de doublons
# ---------------------------------------------------------------------------

def similarite(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def score_completude(row: dict) -> int:
    """Compte les champs remplis — on garde le lead le plus complet."""
    champs = ["phone", "email", "address", "website", "rating"]
    return sum(1 for c in champs if row.get(c))


def detecter_doublons(rows: list[dict]) -> dict[str, str]:
    """
    Retourne un dict {place_id_doublon: place_id_conserve}.
    Seuil de similarité : 0.90 sur le nom normalisé.
    """
    # Trie par nom normalisé pour accélérer (les doublons sont proches)
    triés = sorted(rows, key=lambda r: normaliser(r["name"]))
    doublons: dict[str, str] = {}
    deja_exclus: set[str] = set()

    for i, r1 in enumerate(triés):
        if r1["place_id"] in deja_exclus:
            continue
        n1 = normaliser(r1["name"])
        groupe = [r1]

        for r2 in triés[i + 1:]:
            if r2["place_id"] in deja_exclus:
                continue
            n2 = normaliser(r2["name"])
            # Optimisation : si les 4 premiers chars diffèrent → stop
            if n2[:4] != n1[:4] and abs(len(n1) - len(n2)) > 8:
                break
            if similarite(n1, n2) >= 0.90:
                groupe.append(r2)

        if len(groupe) > 1:
            # Garder le plus complet
            meilleur = max(groupe, key=score_completude)
            for r in groupe:
                if r["place_id"] != meilleur["place_id"]:
                    doublons[r["place_id"]] = meilleur["place_id"]
                    deja_exclus.add(r["place_id"])

    return doublons


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run(dept_filter: str | None, dry_run: bool):
    conn = await asyncpg.connect(_DB_URL)

    # Ajoute le statut 'exclu' s'il n'est pas déjà géré (colonne text, pas d'enum)
    where = "statut = 'nouveau' OR statut IS NULL"
    if dept_filter:
        where += f" AND dept = '{dept_filter}'"

    rows = await conn.fetch(
        f"""SELECT place_id, name, categorie, statut, phone, email,
                   address, website, rating
            FROM landscapers
            WHERE {where}
            ORDER BY name"""
    )

    total     = len(rows)
    a_exclure: dict[str, str] = {}  # place_id → raison

    print(f"{'[DRY-RUN] ' if dry_run else ''}Analyse de {total} leads...")

    # ── Étape 1 : classification par mots-clés ──
    for row in rows:
        decision = classifier(row["name"] or "", row["categorie"])
        if decision == "exclu":
            a_exclure[row["place_id"]] = "hors-cible"

    # ── Étape 2 : détection de doublons (sur leads non déjà exclus) ──
    valides = [r for r in rows if r["place_id"] not in a_exclure]
    doublons = detecter_doublons([dict(r) for r in valides])
    for pid, conserve in doublons.items():
        a_exclure[pid] = f"doublon de {conserve[:8]}…"

    # ── Résultats ──
    nb_hors_cible = sum(1 for r in a_exclure.values() if r == "hors-cible")
    nb_doublons   = len(doublons)
    nb_gardes     = total - len(a_exclure)

    print(f"\n{'─'*50}")
    print(f"  Total analysé  : {total}")
    print(f"  Gardés         : {nb_gardes}")
    print(f"  Hors-cible     : {nb_hors_cible}")
    print(f"  Doublons       : {nb_doublons}")
    print(f"  Total à exclure: {len(a_exclure)}")
    print(f"{'─'*50}")

    # Aperçu des 15 premiers exclus
    if a_exclure:
        print("\nAperçu des exclusions :")
        for pid, raison in list(a_exclure.items())[:15]:
            row = next((r for r in rows if r["place_id"] == pid), None)
            nom = row["name"] if row else pid
            print(f"  - {nom[:50]:<50}  [{raison}]")
        if len(a_exclure) > 15:
            print(f"  ... et {len(a_exclure) - 15} autres")

    # ── Mise à jour ──
    if dry_run:
        print("\n[DRY-RUN] Aucune modification effectuee.")
    elif a_exclure:
        ids = list(a_exclure.keys())
        # Batch update par tranches de 500
        for i in range(0, len(ids), 500):
            batch = ids[i:i + 500]
            await conn.execute(
                "UPDATE landscapers SET statut = 'exclu' WHERE place_id = ANY($1::text[])",
                batch,
            )
        print(f"\n{len(a_exclure)} leads mis a jour -> statut='exclu'")
    else:
        print("\nAucun lead a exclure.")

    await conn.close()


def main():
    parser = argparse.ArgumentParser(description="Nettoyage des leads paysagistes")
    parser.add_argument("--dept",    metavar="XX", help="Limite a un departement (ex: 06)")
    parser.add_argument("--dry-run", action="store_true", help="Analyse sans modifier la base")
    args = parser.parse_args()

    asyncio.run(run(args.dept, args.dry_run))


if __name__ == "__main__":
    main()
