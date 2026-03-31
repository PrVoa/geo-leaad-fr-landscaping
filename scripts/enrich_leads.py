"""
Enrichissement des leads paysagistes via l'API Entreprise du gouvernement.

Usage :
    python enrich_leads.py                  # enrichit tout ce qui manque
    python enrich_leads.py --dept 06        # limite à un département
    python enrich_leads.py --limit 200      # max 200 appels API
    python enrich_leads.py --dry-run        # affiche sans écrire en base
    python enrich_leads.py --delay 1.5      # délai entre appels (défaut 1s)

API utilisée (gratuite, sans clé) :
    https://recherche-entreprises.api.gouv.fr/search
"""
import argparse
import asyncio
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path

# Force UTF-8 sur Windows (évite UnicodeEncodeError avec les accents et barres)
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

API_URL     = "https://recherche-entreprises.api.gouv.fr/search"
API_URL_ALT = "https://api.annuaire-entreprises.data.gouv.fr/search"

# Regex SIRET (14 chiffres, éventuellement groupés par espaces ou tirets)
_SIRET_RE = re.compile(r'\b(\d[\d \-]{12,16}\d)\b')

# Formes juridiques à retirer du nom avant la recherche
_FORMES_RE = re.compile(
    r'\b(SASU|SARL|SAS|EURL|EARL|EI|SCI|SA\b|SNC|SCOP|ASSO|AUTO[ -]?ENTREPRENEUR)\b',
    flags=re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Nettoyage du nom
# ---------------------------------------------------------------------------

def nettoyer_nom(nom: str) -> str:
    """Retire les formes juridiques et espaces superflus."""
    return _FORMES_RE.sub('', nom).strip(' -–—').strip()


def variantes_nom(nom: str) -> list[str]:
    """
    Retourne jusqu'à 3 variantes pour maximiser les chances de match :
    1. Nom original
    2. Nom sans forme juridique
    3. Premier mot significatif (si le nom nettoyé contient plusieurs mots)
    """
    brut    = nom.strip()
    nettoye = nettoyer_nom(brut)
    mots    = [m for m in nettoye.split() if len(m) > 2]
    premier = mots[0] if mots else brut

    vus = []
    for v in [brut, nettoye, premier]:
        v = v.strip()
        if v and v not in vus:
            vus.append(v)
    return vus


# ---------------------------------------------------------------------------
# Score de similarité
# ---------------------------------------------------------------------------

def similarite(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def est_bon_match(nom_recherche: str, result: dict, seuil: float = 0.35) -> bool:
    """Vérifie que le résultat API ressemble au nom cherché."""
    nom_api = (
        result.get("nom_complet")
        or result.get("nom_raison_sociale")
        or result.get("siege", {}).get("denomination")
        or ""
    )
    nom_net = nettoyer_nom(nom_recherche)
    # On accepte si l'un ou l'autre dépasse le seuil
    return (
        similarite(nom_recherche, nom_api) >= seuil
        or similarite(nom_net, nom_api) >= seuil
    )


# ---------------------------------------------------------------------------
# Extraction depuis la réponse API
# ---------------------------------------------------------------------------

def extraire_dirigeant(result: dict) -> tuple[str | None, str | None]:
    """Retourne (prenom, nom) du dirigeant principal."""
    dirigeants = result.get("dirigeants", [])
    if not dirigeants:
        return None, None
    d = dirigeants[0]

    # "prenoms" contient tous les prénoms séparés par espace → on prend le premier
    prenoms_raw = (d.get("prenoms") or d.get("prenom") or "").strip()
    prenom = prenoms_raw.split()[0].title() if prenoms_raw else None

    # "nom" peut contenir "NOM (NOM_USAGE)" → on retire la partie entre parenthèses
    nom_raw = (d.get("nom") or "").strip()
    nom = re.sub(r'\s*\(.*?\)', '', nom_raw).strip().upper() or None

    return prenom, nom


def extraire_forme_juridique(result: dict) -> str | None:
    for key in ("forme_juridique", "libelle_forme_juridique", "libelle_nature_juridique"):
        val = result.get(key)
        if val:
            return val
    return None


def extraire_infos(result: dict) -> dict:
    siege  = result.get("siege", {})
    siren  = result.get("siren", "")
    siret  = siege.get("siret", "") or siren
    date_c = result.get("date_creation") or siege.get("date_creation") or None

    prenom, nom = extraire_dirigeant(result)
    return {
        "siret":           siret or None,
        "forme_juridique": extraire_forme_juridique(result),
        "date_creation":   date_c or None,
        "prenom_gerant":   prenom,
        "nom_gerant":      nom,
    }


# ---------------------------------------------------------------------------
# Appel API avec cascade de variantes
# ---------------------------------------------------------------------------

def extraire_dept_adresse(address: str | None) -> str | None:
    """Extrait le dept depuis l'adresse si le champ dept est vide."""
    if not address:
        return None
    m = re.search(r'\b(\d{5})\b', address)
    return m.group(1)[:2] if m else None


async def tester_api(url: str, timeout: float = 5.0) -> bool:
    """Retourne True si l'URL API répond correctement."""
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(url, params={"q": "test", "limite": 1}, timeout=timeout)
            return r.status_code < 500
    except Exception:
        return False


async def _appel_api(
    client: httpx.AsyncClient,
    q: str,
    dept: str | None,
    url: str = API_URL,
) -> list[dict]:
    params: dict = {"q": q, "limite": 5, "page": 1}
    if dept:
        params["departement"] = dept  # filtre côté API
    try:
        r = await client.get(url, params=params, timeout=10)
        r.raise_for_status()
        return r.json().get("results", [])
    except Exception:
        return []


async def extraire_siret_depuis_site(client: httpx.AsyncClient, website: str) -> str | None:
    """Tente d'extraire un SIRET (14 chiffres) depuis la page d'accueil du site."""
    if not website or not website.startswith("http"):
        return None
    try:
        r = await client.get(website, timeout=8.0, follow_redirects=True)
        # Cherche un bloc de 14 chiffres consécutifs (SIRET)
        for m in _SIRET_RE.finditer(r.text):
            digits = re.sub(r'[\s\-]', '', m.group(1))
            if len(digits) == 14 and digits.isdigit():
                return digits
    except Exception:
        pass
    return None


async def chercher_entreprise(
    client: httpx.AsyncClient,
    nom: str,
    dept: str | None,
    address: str | None = None,
    api_url: str = API_URL,
) -> dict | None:
    """
    Cascade de variantes : nom original → nom nettoyé → premier mot.
    Filtre par département (côté API + vérification code postal).
    Vérifie la similarité avant d'accepter un résultat.
    """
    dept = dept or extraire_dept_adresse(address)

    for variante in variantes_nom(nom):
        results = await _appel_api(client, variante, dept, url=api_url)
        if not results:
            continue

        # Préférer un résultat dans le bon département
        if dept:
            for res in results:
                cp = res.get("siege", {}).get("code_postal", "") or ""
                if cp.startswith(dept) and est_bon_match(nom, res):
                    return res

        # Sinon prendre le premier avec un bon score
        for res in results:
            if est_bon_match(nom, res):
                return res

    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run(dept_filter: str | None, limit: int | None, dry_run: bool, delay: float):
    # ── Test de connectivité API au démarrage ──
    api_url: str | None = None
    print("  Vérification de l'API gouvernementale...", end=" ", flush=True)
    if await tester_api(API_URL):
        api_url = API_URL
        print(f"OK ({API_URL})")
    elif await tester_api(API_URL_ALT):
        api_url = API_URL_ALT
        print(f"OK (fallback: {API_URL_ALT})")
    else:
        print("INACCESSIBLE")
        print("⚠️  API gouvernementale inaccessible - enrichissement API désactivé (fallback SIRET depuis site web uniquement)")

    conn = await asyncpg.connect(_DB_URL)

    # Crée les colonnes si elles n'existent pas encore
    await conn.execute("""
        ALTER TABLE landscapers
          ADD COLUMN IF NOT EXISTS prenom_gerant   TEXT,
          ADD COLUMN IF NOT EXISTS nom_gerant      TEXT,
          ADD COLUMN IF NOT EXISTS siret           TEXT,
          ADD COLUMN IF NOT EXISTS forme_juridique TEXT,
          ADD COLUMN IF NOT EXISTS date_creation   TEXT
    """)

    where = "nom_gerant IS NULL AND name IS NOT NULL"
    if dept_filter:
        where += f" AND dept = '{dept_filter}'"

    rows = await conn.fetch(
        f"SELECT place_id, name, dept, address, website FROM landscapers WHERE {where} ORDER BY scraped_at DESC"
        + (f" LIMIT {limit}" if limit else "")
    )

    total         = len(rows)
    enrichis      = 0
    echecs        = 0
    siret_website = 0

    print(f"{'[DRY-RUN] ' if dry_run else ''}>> {total} leads a enrichir")
    if not total:
        await conn.close()
        return

    async with httpx.AsyncClient(headers={"User-Agent": "geo-leaad-enrichissement/1.0"}) as http:
        for i, row in enumerate(rows, 1):
            nom     = row["name"]
            pid     = row["place_id"]
            dept    = row["dept"]
            address = row["address"]
            website = row["website"] if "website" in row.keys() else None

            pct = int(i / total * 30)
            bar = "#" * pct + "-" * (30 - pct)
            print(f"\r[{bar}] {i}/{total}  {nom[:40]:<40}", end="", flush=True)

            result = None
            if api_url:
                result = await chercher_entreprise(http, nom, dept, address, api_url=api_url)

            if result:
                infos = extraire_infos(result)
                if not dry_run:
                    await conn.execute(
                        """UPDATE landscapers SET
                            prenom_gerant   = $1,
                            nom_gerant      = $2,
                            siret           = $3,
                            forme_juridique = $4,
                            date_creation   = $5
                        WHERE place_id = $6""",
                        infos["prenom_gerant"],
                        infos["nom_gerant"],
                        infos["siret"],
                        infos["forme_juridique"],
                        infos["date_creation"],
                        pid,
                    )
                enrichis += 1
            else:
                # Fallback : tenter d'extraire le SIRET depuis le site web
                if website:
                    siret = await extraire_siret_depuis_site(http, website)
                    if siret:
                        if not dry_run:
                            await conn.execute(
                                "UPDATE landscapers SET siret = $1 WHERE place_id = $2",
                                siret, pid,
                            )
                        siret_website += 1
                        enrichis += 1
                    else:
                        echecs += 1
                else:
                    echecs += 1

            await asyncio.sleep(delay)

    await conn.close()
    print()
    pct_enrichis = round(enrichis / total * 100) if total else 0
    print(f"\nEnrichis : {enrichis}/{total} ({pct_enrichis}%)  |  Non trouves : {echecs}")
    if siret_website:
        print(f"   dont {siret_website} via extraction site web (SIRET uniquement)")
    if dry_run:
        print("   (dry-run -- aucune ecriture en base)")


def main():
    parser = argparse.ArgumentParser(description="Enrichissement leads via API Entreprise")
    parser.add_argument("--dept",    metavar="XX",  help="Limite a un departement (ex: 06)")
    parser.add_argument("--limit",   metavar="N",   type=int, help="Nombre max de leads a traiter")
    parser.add_argument("--dry-run", action="store_true", help="Simule sans ecrire en base")
    parser.add_argument("--delay",   metavar="SEC", type=float, default=1.0,
                        help="Delai entre appels API (defaut: 1s)")
    args = parser.parse_args()

    asyncio.run(run(args.dept, args.limit, args.dry_run, args.delay))


if __name__ == "__main__":
    main()
