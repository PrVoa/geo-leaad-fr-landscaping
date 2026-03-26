"""
Enrichissement des leads paysagistes via l'API Entreprise du gouvernement.

Usage :
    python enrich_leads.py                  # enrichit tout ce qui manque
    python enrich_leads.py --dept 06        # limite à un département
    python enrich_leads.py --limit 200      # max 200 appels API
    python enrich_leads.py --dry-run        # affiche sans écrire en base
    python enrich_leads.py --delay 1.5      # délai entre appels (défaut 1s)

API utilisée (gratuite, sans clé) :
    https://recherche-entreprises.api.gouv.fr/search?q=NOM&limite=1
"""
import argparse
import asyncio
import re
import sys
from pathlib import Path

import asyncpg
import httpx
from dotenv import load_dotenv
import os

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

DATABASE_URL = os.getenv("DATABASE_URL", "")
if not DATABASE_URL:
    sys.exit("❌  DATABASE_URL manquant dans .env")

_DB_URL = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://").replace(
    "postgres+asyncpg://", "postgresql://"
)

API_URL = "https://recherche-entreprises.api.gouv.fr/search"


# ---------------------------------------------------------------------------
# Extraction depuis la réponse API
# ---------------------------------------------------------------------------

def extraire_dirigeant(result: dict) -> str | None:
    """Retourne 'Prénom NOM' du dirigeant principal, ou None."""
    dirigeants = result.get("dirigeants", [])
    if not dirigeants:
        return None
    d = dirigeants[0]
    prenom = (d.get("prenom") or "").strip().title()
    nom    = (d.get("nom")    or "").strip().upper()
    qualite = d.get("qualite", "")
    if nom:
        return f"{prenom} {nom}".strip() if prenom else nom
    return None


def extraire_infos(result: dict) -> dict:
    """Extrait siret, forme_juridique, date_creation et nom_gerant."""
    siege = result.get("siege", {})
    libelle_forme = (
        result.get("nature_juridique")             # code (ex: 5499)
        or result.get("libelle_nature_juridique")  # libellé long
        or None
    )
    # Chercher le libellé lisible dans les champs alternatifs
    for key in ("forme_juridique", "libelle_forme_juridique"):
        val = result.get(key)
        if val:
            libelle_forme = val
            break

    siren  = result.get("siren", "")
    siret  = siege.get("siret", "") or siren  # fallback sur siren
    date_c = result.get("date_creation") or siege.get("date_creation") or None

    return {
        "siret":           siret or None,
        "forme_juridique": libelle_forme or None,
        "date_creation":   date_c or None,
        "nom_gerant":      extraire_dirigeant(result),
    }


# ---------------------------------------------------------------------------
# Appel API
# ---------------------------------------------------------------------------

async def chercher_entreprise(
    client: httpx.AsyncClient,
    nom: str,
    dept: str | None,
) -> dict | None:
    """
    Interroge l'API et retourne le premier résultat pertinent, ou None.
    Si dept est connu, filtre par code_postal commençant par dept.
    """
    params = {"q": nom, "limite": 5, "page": 1}
    try:
        r = await client.get(API_URL, params=params, timeout=10)
        r.raise_for_status()
    except httpx.HTTPError:
        return None

    results = r.json().get("results", [])
    if not results:
        return None

    # Filtre par département si on le connaît
    if dept:
        for res in results:
            cp = res.get("siege", {}).get("code_postal", "") or ""
            if cp.startswith(dept):
                return res

    return results[0]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run(dept_filter: str | None, limit: int | None, dry_run: bool, delay: float):
    conn = await asyncpg.connect(_DB_URL)

    # Récupère les leads sans nom_gerant
    where = "nom_gerant IS NULL AND name IS NOT NULL"
    if dept_filter:
        where += f" AND dept = '{dept_filter}'"

    rows = await conn.fetch(
        f"SELECT place_id, name, dept FROM landscapers WHERE {where} ORDER BY scraped_at DESC"
        + (f" LIMIT {limit}" if limit else "")
    )

    total   = len(rows)
    enrichis = 0
    echecs   = 0

    print(f"{'[DRY-RUN] ' if dry_run else ''}🔍  {total} leads à enrichir")
    if not total:
        await conn.close()
        return

    async with httpx.AsyncClient(headers={"User-Agent": "geo-leaad-enrichissement/1.0"}) as http:
        for i, row in enumerate(rows, 1):
            nom   = row["name"]
            pid   = row["place_id"]
            dept  = row["dept"]

            # Barre de progression
            pct = int(i / total * 30)
            bar = "█" * pct + "░" * (30 - pct)
            print(f"\r[{bar}] {i}/{total}  {nom[:40]:<40}", end="", flush=True)

            result = await chercher_entreprise(http, nom, dept)
            if result:
                infos = extraire_infos(result)
                if not dry_run:
                    await conn.execute(
                        """UPDATE landscapers SET
                            nom_gerant      = $1,
                            siret           = $2,
                            forme_juridique = $3,
                            date_creation   = $4
                        WHERE place_id = $5""",
                        infos["nom_gerant"],
                        infos["siret"],
                        infos["forme_juridique"],
                        infos["date_creation"],
                        pid,
                    )
                enrichis += 1
            else:
                echecs += 1

            await asyncio.sleep(delay)

    await conn.close()
    print()  # saut de ligne après la barre
    print(f"\n✅  Enrichis : {enrichis}  |  ❌ Non trouvés : {echecs}  |  Total traité : {total}")
    if dry_run:
        print("   (dry-run — aucune écriture en base)")


def main():
    parser = argparse.ArgumentParser(description="Enrichissement leads via API Entreprise")
    parser.add_argument("--dept",    metavar="XX",  help="Limite à un département (ex: 06)")
    parser.add_argument("--limit",   metavar="N",   type=int, help="Nombre max de leads à traiter")
    parser.add_argument("--dry-run", action="store_true", help="Simule sans écrire en base")
    parser.add_argument("--delay",   metavar="SEC", type=float, default=1.0, help="Délai entre appels API (défaut: 1s)")
    args = parser.parse_args()

    asyncio.run(run(args.dept, args.limit, args.dry_run, args.delay))


if __name__ == "__main__":
    main()
