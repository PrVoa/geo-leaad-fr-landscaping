"""
Enrichissement des leads paysagistes via scraping de societe.com (Playwright).

Pour chaque lead sans nom_gerant (statut != 'exclu') :
  1. Recherche sur societe.com
  2. Clique sur le premier résultat pertinent
  3. Extrait : nom_gerant, siret, forme_juridique, code_naf, statut actif/radié
  4. Met à jour Supabase
  5. Si radié → statut = 'exclu'

Usage :
    python enrich_societe.py              # enrichit tous les leads manquants
    python enrich_societe.py --limit 500  # limite à 500 leads
    python enrich_societe.py --stats      # statistiques uniquement
    python enrich_societe.py --dept 06    # un département seulement
    python enrich_societe.py --dry-run    # affiche sans écrire en base
"""
import argparse
import asyncio
import logging
import os
import random
import re
import sys
import time
import unicodedata
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import asyncpg
from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PWTimeoutError

try:
    from playwright_stealth import Stealth
    STEALTH_AVAILABLE = True
except ImportError:
    STEALTH_AVAILABLE = False

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

DATABASE_URL = os.getenv("DATABASE_URL", "")
if not DATABASE_URL:
    sys.exit("DATABASE_URL manquant dans .env")

_DB_URL = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://").replace(
    "postgres+asyncpg://", "postgresql://"
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_FILE = Path(__file__).resolve().parent.parent / "logs" / "enrich_societe.log"
LOG_FILE.parent.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("enrich_societe")

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

SEARCH_URL   = "https://www.societe.com/cgi-bin/search?champs={query}"
DELAY_MIN    = 3.0
DELAY_MAX    = 6.0
BROWSER_RESTART_EVERY = 100   # fiches
CAPTCHA_PAUSE_SECONDS = 600   # 10 min

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

# Regex
_SIRET_RE    = re.compile(r'\b(\d{3}[\s\u00a0]?\d{3}[\s\u00a0]?\d{3}[\s\u00a0]?\d{5})\b')
_SIREN_RE    = re.compile(r'\b(\d{3}[\s\u00a0]?\d{3}[\s\u00a0]?\d{3})\b')
_NAF_RE      = re.compile(r'\b(\d{4}[A-Z])\b')
_FORMES_RE   = re.compile(
    r'\b(SASU|SARL|SAS|EURL|EARL|EI|SCI|SA\b|SNC|SCOP|AUTO[ -]?ENTREPRENEUR|ENTREPRISE INDIVIDUELLE)\b',
    flags=re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Normalisation / similarité
# ---------------------------------------------------------------------------

def normaliser(texte: str) -> str:
    t = texte.lower().strip()
    t = unicodedata.normalize("NFD", t)
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def similarite(a: str, b: str) -> float:
    return SequenceMatcher(None, normaliser(a), normaliser(b)).ratio()


def nettoyer_nom(nom: str) -> str:
    return _FORMES_RE.sub("", nom).strip(" -–—").strip()

# ---------------------------------------------------------------------------
# Extraction depuis la fiche societe.com
# ---------------------------------------------------------------------------

def extraire_siret(texte: str) -> str | None:
    """Extrait un SIRET (14 chiffres) depuis un bloc de texte."""
    for m in _SIRET_RE.finditer(texte):
        digits = re.sub(r'[\s\u00a0]', '', m.group(1))
        if len(digits) == 14:
            return digits
    return None


def extraire_naf(texte: str) -> str | None:
    m = _NAF_RE.search(texte)
    return m.group(1) if m else None


def extraire_forme_juridique(texte: str) -> str | None:
    m = _FORMES_RE.search(texte)
    return m.group(0).upper() if m else None


async def extraire_dirigeant(page) -> tuple[str | None, str | None]:
    """
    Extrait (prenom, nom) du dirigeant principal depuis la fiche societe.com.
    Cherche dans le bloc dirigeants / mandataires.
    """
    # Sélecteurs possibles sur societe.com
    selectors = [
        "div.dirigeant a",
        "div#dirigeants a",
        "section.dirigeants a",
        "td.identite a",
        "div.fiche-identite a[href*='/personne/']",
        "a[href*='/personne/']",
    ]
    for sel in selectors:
        try:
            els = await page.query_selector_all(sel)
            for el in els:
                texte = (await el.inner_text()).strip()
                # Format attendu : "DUPONT Jean" ou "Jean DUPONT"
                if not texte or len(texte) < 3:
                    continue
                parts = texte.split()
                if len(parts) >= 2:
                    # Heuristique : le NOM est en majuscules
                    noms    = [p for p in parts if p.isupper() and len(p) > 1]
                    prenoms = [p for p in parts if not p.isupper() or len(p) == 1]
                    if noms:
                        nom    = " ".join(noms)
                        prenom = " ".join(prenoms).title() or None
                        return prenom, nom
        except Exception:
            continue

    # Fallback : chercher dans le texte brut de la section dirigeants
    try:
        section = await page.query_selector("div#dirigeants, section.dirigeants, div.dirigeant")
        if section:
            texte = await section.inner_text()
            lignes = [l.strip() for l in texte.splitlines() if l.strip()]
            for ligne in lignes:
                parts = ligne.split()
                if 2 <= len(parts) <= 4:
                    noms = [p for p in parts if p.isupper() and len(p) > 1]
                    if noms:
                        prenoms = [p for p in parts if p not in noms]
                        return (" ".join(prenoms).title() or None), " ".join(noms)
    except Exception:
        pass

    return None, None


async def extraire_fiche(page) -> dict:
    """
    Extrait toutes les infos utiles depuis la page fiche ouverte.
    Retourne un dict avec les clés : siret, forme_juridique, code_naf,
    prenom_gerant, nom_gerant, radie.
    """
    try:
        texte_page = await page.inner_text("body")
    except Exception:
        texte_page = ""

    prenom, nom = await extraire_dirigeant(page)

    siret = extraire_siret(texte_page)
    naf   = extraire_naf(texte_page)

    # Forme juridique : tenter d'abord un sélecteur dédié
    forme = None
    try:
        for sel in ["td.forme-juridique", "span.forme-juridique", "div.forme-juridique"]:
            el = await page.query_selector(sel)
            if el:
                forme = (await el.inner_text()).strip().upper() or None
                break
    except Exception:
        pass
    if not forme:
        forme = extraire_forme_juridique(texte_page)

    # Statut radié : chercher des marqueurs textuels
    texte_lower = texte_page.lower()
    radie = any(kw in texte_lower for kw in [
        "radiée", "radiee", "fermée", "fermee", "cessation d'activité",
        "cessation d activite", "entreprise radiée", "établissement fermé",
    ])

    return {
        "siret":           siret,
        "forme_juridique": forme,
        "code_naf":        naf,
        "prenom_gerant":   prenom,
        "nom_gerant":      nom,
        "radie":           radie,
    }

# ---------------------------------------------------------------------------
# Détection captcha
# ---------------------------------------------------------------------------

async def detecter_captcha(page) -> bool:
    url   = page.url.lower()
    titre = (await page.title()).lower()
    if any(kw in url   for kw in ["captcha", "challenge", "robot", "blocked"]):
        return True
    if any(kw in titre for kw in ["captcha", "attention", "vérification", "blocked", "robot"]):
        return True
    try:
        texte = await page.inner_text("body")
        if any(kw in texte.lower() for kw in [
            "êtes-vous un robot", "are you a robot",
            "vérification de sécurité", "security check",
            "please verify", "verifiez que vous",
        ]):
            return True
    except Exception:
        pass
    return False

# ---------------------------------------------------------------------------
# Scraping d'une fiche
# ---------------------------------------------------------------------------

async def scraper_lead(page, nom: str, dept: str | None) -> dict | None:
    """
    Recherche le lead sur societe.com et extrait ses données.
    Retourne None si aucun résultat pertinent.
    """
    query = nom.strip()
    if dept:
        query = f"{query} {dept}"
    url = SEARCH_URL.format(query=query.replace(" ", "+"))

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
    except PWTimeoutError:
        log.warning("Timeout chargement recherche : %s", nom[:50])
        return None
    except Exception as exc:
        log.warning("Erreur goto recherche %s : %s", nom[:50], exc)
        return None

    if await detecter_captcha(page):
        return "CAPTCHA"

    # ── Trouver le premier résultat pertinent ──
    lien_fiche = None
    try:
        # Liens de résultats sur societe.com : /societe/NOM-SIREN.html
        resultats = await page.query_selector_all("a[href*='/societe/']")
        for el in resultats:
            href  = await el.get_attribute("href") or ""
            texte = (await el.inner_text()).strip()
            if not href or not texte:
                continue
            # Filtrer les liens de nav (ex: /societe/recherche)
            if re.search(r'/societe/[^/]+-\d{9}', href):
                score = similarite(nom, texte)
                if score >= 0.30:
                    lien_fiche = href if href.startswith("http") else f"https://www.societe.com{href}"
                    break
    except Exception as exc:
        log.debug("Erreur sélection résultat : %s", exc)

    if not lien_fiche:
        log.debug("Aucun résultat pertinent pour : %s", nom[:50])
        return None

    # ── Ouvrir la fiche ──
    try:
        await page.goto(lien_fiche, wait_until="domcontentloaded", timeout=20_000)
    except PWTimeoutError:
        log.warning("Timeout chargement fiche : %s", nom[:50])
        return None
    except Exception as exc:
        log.warning("Erreur goto fiche %s : %s", nom[:50], exc)
        return None

    if await detecter_captcha(page):
        return "CAPTCHA"

    return await extraire_fiche(page)

# ---------------------------------------------------------------------------
# Gestion du navigateur
# ---------------------------------------------------------------------------

async def creer_contexte(playwright):
    """Crée un nouveau contexte Playwright avec user-agent aléatoire."""
    ua = random.choice(USER_AGENTS)
    browser = await playwright.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
        ],
    )
    context = await browser.new_context(
        user_agent=ua,
        viewport={"width": random.randint(1280, 1920), "height": random.randint(800, 1080)},
        locale="fr-FR",
        timezone_id="Europe/Paris",
        extra_http_headers={
            "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        },
    )

    if STEALTH_AVAILABLE:
        try:
            stealth = Stealth(navigator_user_agent_override=ua)
            await stealth.apply_stealth_async(context)
        except Exception as exc:
            log.debug("Stealth non appliqué : %s", exc)

    page = await context.new_page()
    # Bloquer les ressources inutiles pour accélérer
    await page.route("**/*.{png,jpg,jpeg,gif,svg,ico,woff,woff2,ttf,mp4,webp}", lambda r: r.abort())
    return browser, context, page


async def fermer_browser(browser):
    try:
        await browser.close()
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

async def afficher_stats(conn):
    total   = await conn.fetchval("SELECT COUNT(*) FROM landscapers WHERE statut != 'exclu'")
    enrichis = await conn.fetchval(
        "SELECT COUNT(*) FROM landscapers WHERE nom_gerant IS NOT NULL AND statut != 'exclu'"
    )
    manquants = total - enrichis
    pct = round(enrichis / total * 100) if total else 0
    print(f"\n{'─' * 50}")
    print(f"  Total leads actifs    : {total}")
    print(f"  Enrichis (nom_gerant) : {enrichis}  ({pct}%)")
    print(f"  À enrichir            : {manquants}")
    print(f"{'─' * 50}\n")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run(dept_filter: str | None, limit: int | None, dry_run: bool):
    conn = await asyncpg.connect(_DB_URL)

    # Créer les colonnes manquantes
    await conn.execute("""
        ALTER TABLE landscapers
          ADD COLUMN IF NOT EXISTS prenom_gerant   TEXT,
          ADD COLUMN IF NOT EXISTS nom_gerant      TEXT,
          ADD COLUMN IF NOT EXISTS siret           TEXT,
          ADD COLUMN IF NOT EXISTS forme_juridique TEXT,
          ADD COLUMN IF NOT EXISTS date_creation   TEXT,
          ADD COLUMN IF NOT EXISTS code_naf        TEXT
    """)

    where = "nom_gerant IS NULL AND name IS NOT NULL AND (statut IS NULL OR statut != 'exclu')"
    if dept_filter:
        where += f" AND dept = '{dept_filter}'"

    rows = await conn.fetch(
        f"SELECT place_id, name, dept FROM landscapers WHERE {where} ORDER BY scraped_at DESC"
        + (f" LIMIT {limit}" if limit else "")
    )

    total = len(rows)
    if not total:
        print("Aucun lead à enrichir.")
        await conn.close()
        return

    prefix = "[DRY-RUN] " if dry_run else ""
    print(f"\n{prefix}>> {total} leads à enrichir via societe.com\n")

    enrichis   = 0
    exclus     = 0
    echecs     = 0
    debut      = time.time()
    fiches_session = 0

    async with async_playwright() as pw:
        browser, context, page = await creer_contexte(pw)

        for idx, row in enumerate(rows, 1):
            nom     = row["name"] or ""
            pid     = row["place_id"]
            dept    = row["dept"]

            # Redémarrer le browser toutes les N fiches
            if fiches_session >= BROWSER_RESTART_EVERY:
                log.info("Redémarrage du browser après %d fiches", fiches_session)
                await fermer_browser(browser)
                await asyncio.sleep(random.uniform(2, 5))
                browser, context, page = await creer_contexte(pw)
                fiches_session = 0

            result = await scraper_lead(page, nom, dept)
            fiches_session += 1

            # ── Captcha détecté ──
            if result == "CAPTCHA":
                log.warning("CAPTCHA détecté — pause %d min", CAPTCHA_PAUSE_SECONDS // 60)
                await fermer_browser(browser)
                await asyncio.sleep(CAPTCHA_PAUSE_SECONDS)
                browser, context, page = await creer_contexte(pw)
                fiches_session = 0
                # Réessayer ce lead
                result = await scraper_lead(page, nom, dept)
                fiches_session += 1
                if result == "CAPTCHA":
                    log.error("CAPTCHA persistant — abandon du lead : %s", nom[:50])
                    echecs += 1
                    continue

            if not result:
                echecs += 1
            else:
                if not dry_run:
                    nouveau_statut = "exclu" if result["radie"] else None

                    if nouveau_statut == "exclu":
                        await conn.execute(
                            """UPDATE landscapers SET
                                prenom_gerant   = $1,
                                nom_gerant      = $2,
                                siret           = $3,
                                forme_juridique = $4,
                                code_naf        = $5,
                                statut          = 'exclu'
                            WHERE place_id = $6""",
                            result["prenom_gerant"],
                            result["nom_gerant"],
                            result["siret"],
                            result["forme_juridique"],
                            result["code_naf"],
                            pid,
                        )
                        exclus += 1
                        log.info("RADIÉ→EXCLU  %s", nom[:60])
                    else:
                        await conn.execute(
                            """UPDATE landscapers SET
                                prenom_gerant   = $1,
                                nom_gerant      = $2,
                                siret           = $3,
                                forme_juridique = $4,
                                code_naf        = $5
                            WHERE place_id = $6""",
                            result["prenom_gerant"],
                            result["nom_gerant"],
                            result["siret"],
                            result["forme_juridique"],
                            result["code_naf"],
                            pid,
                        )

                    enrichis += 1
                    gerant = f"{result['prenom_gerant'] or ''} {result['nom_gerant'] or ''}".strip()
                    log.info("OK  %-55s  %s  SIRET:%s",
                             nom[:55], gerant or "(gérant non trouvé)", result["siret"] or "-")
                else:
                    enrichis += 1

            # ── Progression toutes les 10 fiches ──
            if idx % 10 == 0 or idx == total:
                elapsed  = time.time() - debut
                vitesse  = round(enrichis / elapsed * 3600) if elapsed > 1 else 0
                pct      = round(idx / total * 100)
                bar_fill = int(pct / 5)
                bar      = "█" * bar_fill + "░" * (20 - bar_fill)
                print(
                    f"  [{bar}] {idx}/{total} ({pct}%)  "
                    f"enrichis:{enrichis}  exclus:{exclus}  échecs:{echecs}  "
                    f"~{vitesse}/h",
                    flush=True,
                )

            # Délai aléatoire anti-blocage
            await asyncio.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

        await fermer_browser(browser)

    await conn.close()

    elapsed = time.time() - debut
    print(f"\n{'─' * 60}")
    print(f"  Total traités : {total}")
    print(f"  Enrichis      : {enrichis}")
    print(f"  Radiés→exclus : {exclus}")
    print(f"  Non trouvés   : {echecs}")
    print(f"  Durée         : {int(elapsed // 60)}m{int(elapsed % 60):02d}s")
    if dry_run:
        print("  (dry-run — aucune écriture en base)")
    print(f"{'─' * 60}\n")


def main():
    parser = argparse.ArgumentParser(description="Enrichissement leads via societe.com (Playwright)")
    parser.add_argument("--dept",    metavar="XX",  help="Limite à un département (ex: 06)")
    parser.add_argument("--limit",   metavar="N",   type=int, help="Nombre max de leads à traiter")
    parser.add_argument("--dry-run", action="store_true", help="Simule sans écrire en base")
    parser.add_argument("--stats",   action="store_true", help="Affiche uniquement les statistiques")
    args = parser.parse_args()

    if args.stats:
        async def show():
            conn = await asyncpg.connect(_DB_URL)
            await afficher_stats(conn)
            await conn.close()
        asyncio.run(show())
        return

    asyncio.run(run(args.dept, args.limit, args.dry_run))


if __name__ == "__main__":
    main()
