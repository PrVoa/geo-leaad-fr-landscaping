"""
Enrichissement unifié des leads paysagistes.

Stratégie (par lead, si nom_gerant IS NULL) :
  1. Tentative via societe.com (Playwright) — plus fiable
  2. Fallback sur l'API gouvernementale Entreprise (httpx)
  3. Skip si déjà enrichi (nom_gerant non null)

Usage :
    python enrich.py                  # enrichit tout ce qui manque
    python enrich.py --dept 06        # limite à un département
    python enrich.py --limit 200      # max 200 leads
    python enrich.py --dry-run        # affiche sans écrire en base
    python enrich.py --stats          # statistiques uniquement
    python enrich.py --delay 1.5      # délai inter-appels API (défaut 1s)
"""
import argparse
import asyncio
import random
import re
import sys
import time
import unicodedata
from difflib import SequenceMatcher
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import asyncpg
import httpx
from playwright.async_api import async_playwright, TimeoutError as PWTimeoutError

try:
    from playwright_stealth import Stealth
    STEALTH_AVAILABLE = True
except ImportError:
    STEALTH_AVAILABLE = False

from config import DB_URL as _DB_URL, get_logger

log = get_logger("enrich")

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

SEARCH_URL_SOCIETE    = "https://www.societe.com/cgi-bin/search?champs={query}"
API_URL               = "https://recherche-entreprises.api.gouv.fr/search"
API_URL_ALT           = "https://api.annuaire-entreprises.data.gouv.fr/search"
DELAY_MIN             = 3.0
DELAY_MAX             = 6.0
BROWSER_RESTART_EVERY = 100
CAPTCHA_PAUSE_SECONDS = 600

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

# Regex
_SIRET_RE_SOCIETE = re.compile(r'\b(\d{3}[\s\u00a0]?\d{3}[\s\u00a0]?\d{3}[\s\u00a0]?\d{5})\b')
_SIRET_RE_API     = re.compile(r'\b(\d[\d \-]{12,16}\d)\b')
_NAF_RE           = re.compile(r'\b(\d{4}[A-Z])\b')
_FORMES_RE        = re.compile(
    r'\b(SASU|SARL|SAS|EURL|EARL|EI|SCI|SA\b|SNC|SCOP|AUTO[ -]?ENTREPRENEUR|ENTREPRISE INDIVIDUELLE)\b',
    flags=re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Utilitaires communs
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


def variantes_nom(nom: str) -> list[str]:
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
# MÉTHODE 1 : societe.com via Playwright
# ---------------------------------------------------------------------------

def _extraire_siret_societe(texte: str) -> str | None:
    for m in _SIRET_RE_SOCIETE.finditer(texte):
        digits = re.sub(r'[\s\u00a0]', '', m.group(1))
        if len(digits) == 14:
            return digits
    return None


def _extraire_naf(texte: str) -> str | None:
    m = _NAF_RE.search(texte)
    return m.group(1) if m else None


def _extraire_forme_juridique_texte(texte: str) -> str | None:
    m = _FORMES_RE.search(texte)
    return m.group(0).upper() if m else None


async def _extraire_dirigeant_societe(page) -> tuple[str | None, str | None]:
    """Extrait (prenom, nom) du dirigeant principal depuis la fiche societe.com."""
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
                if not texte or len(texte) < 3:
                    continue
                parts = texte.split()
                if len(parts) >= 2:
                    noms    = [p for p in parts if p.isupper() and len(p) > 1]
                    prenoms = [p for p in parts if not p.isupper() or len(p) == 1]
                    if noms:
                        return (" ".join(prenoms).title() or None), " ".join(noms)
        except Exception:
            continue

    # Fallback : chercher dans le texte brut de la section dirigeants
    try:
        section = await page.query_selector("div#dirigeants, section.dirigeants, div.dirigeant")
        if section:
            texte = await section.inner_text()
            for ligne in [l.strip() for l in texte.splitlines() if l.strip()]:
                parts = ligne.split()
                if 2 <= len(parts) <= 4:
                    noms = [p for p in parts if p.isupper() and len(p) > 1]
                    if noms:
                        prenoms = [p for p in parts if p not in noms]
                        return (" ".join(prenoms).title() or None), " ".join(noms)
    except Exception:
        pass

    return None, None


async def _extraire_fiche_societe(page) -> dict:
    try:
        texte_page = await page.inner_text("body")
    except Exception:
        texte_page = ""

    prenom, nom = await _extraire_dirigeant_societe(page)
    siret = _extraire_siret_societe(texte_page)
    naf   = _extraire_naf(texte_page)

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
        forme = _extraire_forme_juridique_texte(texte_page)

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
        "source":          "societe.com",
    }


async def _detecter_captcha_societe(page) -> bool:
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
        ]):
            return True
    except Exception:
        pass
    return False


async def scraper_lead_societe(page, nom: str, dept: str | None) -> dict | None:
    """Recherche un lead sur societe.com. Retourne None ou 'CAPTCHA'."""
    query = f"{nom.strip()} {dept}" if dept else nom.strip()
    url = SEARCH_URL_SOCIETE.format(query=query.replace(" ", "+"))

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
    except (PWTimeoutError, Exception) as exc:
        log.warning("Timeout/erreur recherche societe.com : %s — %s", nom[:50], exc)
        return None

    if await _detecter_captcha_societe(page):
        return "CAPTCHA"

    lien_fiche = None
    try:
        resultats = await page.query_selector_all("a[href*='/societe/']")
        for el in resultats:
            href  = await el.get_attribute("href") or ""
            texte = (await el.inner_text()).strip()
            if not href or not texte:
                continue
            if re.search(r'/societe/[^/]+-\d{9}', href):
                if similarite(nom, texte) >= 0.30:
                    lien_fiche = href if href.startswith("http") else f"https://www.societe.com{href}"
                    break
    except Exception as exc:
        log.debug("Erreur sélection résultat societe.com : %s", exc)

    if not lien_fiche:
        log.debug("Aucun résultat pertinent societe.com pour : %s", nom[:50])
        return None

    try:
        await page.goto(lien_fiche, wait_until="domcontentloaded", timeout=20_000)
    except (PWTimeoutError, Exception) as exc:
        log.warning("Timeout/erreur fiche societe.com : %s — %s", nom[:50], exc)
        return None

    if await _detecter_captcha_societe(page):
        return "CAPTCHA"

    return await _extraire_fiche_societe(page)


async def creer_contexte_societe(playwright):
    ua = random.choice(USER_AGENTS)
    browser = await playwright.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage"],
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
            await Stealth(navigator_user_agent_override=ua).apply_stealth_async(context)
        except Exception as exc:
            log.debug("Stealth non appliqué : %s", exc)
    page = await context.new_page()
    await page.route("**/*.{png,jpg,jpeg,gif,svg,ico,woff,woff2,ttf,mp4,webp}", lambda r: r.abort())
    return browser, context, page


async def fermer_browser(browser):
    try:
        await browser.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# MÉTHODE 2 : API gouvernementale (fallback)
# ---------------------------------------------------------------------------

def _extraire_dirigeant_api(result: dict) -> tuple[str | None, str | None]:
    dirigeants = result.get("dirigeants", [])
    if not dirigeants:
        return None, None
    d = dirigeants[0]
    prenoms_raw = (d.get("prenoms") or d.get("prenom") or "").strip()
    prenom = prenoms_raw.split()[0].title() if prenoms_raw else None
    nom_raw = (d.get("nom") or "").strip()
    nom = re.sub(r'\s*\(.*?\)', '', nom_raw).strip().upper() or None
    return prenom, nom


def _extraire_infos_api(result: dict) -> dict:
    siege  = result.get("siege", {})
    siren  = result.get("siren", "")
    siret  = siege.get("siret", "") or siren
    date_c = result.get("date_creation") or siege.get("date_creation") or None
    prenom, nom = _extraire_dirigeant_api(result)

    forme = None
    for key in ("forme_juridique", "libelle_forme_juridique", "libelle_nature_juridique"):
        val = result.get(key)
        if val:
            forme = val
            break

    return {
        "siret":           siret or None,
        "forme_juridique": forme,
        "code_naf":        None,
        "prenom_gerant":   prenom,
        "nom_gerant":      nom,
        "radie":           False,
        "date_creation":   date_c or None,
        "source":          "api-gouvernement",
    }


def _extraire_dept_adresse(address: str | None) -> str | None:
    if not address:
        return None
    m = re.search(r'\b(\d{5})\b', address)
    return m.group(1)[:2] if m else None


async def _tester_api(url: str, timeout: float = 5.0) -> bool:
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
    url: str,
) -> list[dict]:
    params: dict = {"q": q, "limite": 5, "page": 1}
    if dept:
        params["departement"] = dept
    try:
        r = await client.get(url, params=params, timeout=10)
        r.raise_for_status()
        return r.json().get("results", [])
    except Exception:
        return []


def _est_bon_match(nom_recherche: str, result: dict, seuil: float = 0.35) -> bool:
    nom_api = (
        result.get("nom_complet")
        or result.get("nom_raison_sociale")
        or result.get("siege", {}).get("denomination")
        or ""
    )
    nom_net = nettoyer_nom(nom_recherche)
    return (
        similarite(nom_recherche, nom_api) >= seuil
        or similarite(nom_net, nom_api) >= seuil
    )


async def chercher_entreprise_api(
    client: httpx.AsyncClient,
    nom: str,
    dept: str | None,
    address: str | None = None,
    api_url: str = API_URL,
) -> dict | None:
    dept = dept or _extraire_dept_adresse(address)
    for variante in variantes_nom(nom):
        results = await _appel_api(client, variante, dept, url=api_url)
        if not results:
            continue
        if dept:
            for res in results:
                cp = res.get("siege", {}).get("code_postal", "") or ""
                if cp.startswith(dept) and _est_bon_match(nom, res):
                    return res
        for res in results:
            if _est_bon_match(nom, res):
                return res
    return None


async def extraire_siret_depuis_site(client: httpx.AsyncClient, website: str) -> str | None:
    if not website or not website.startswith("http"):
        return None
    try:
        r = await client.get(website, timeout=8.0, follow_redirects=True)
        for m in _SIRET_RE_API.finditer(r.text):
            digits = re.sub(r'[\s\-]', '', m.group(1))
            if len(digits) == 14 and digits.isdigit():
                return digits
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Enrichissement unifié d'un lead
# ---------------------------------------------------------------------------

async def enrichir_lead(
    page,
    http: httpx.AsyncClient,
    nom: str,
    dept: str | None,
    website: str | None,
    api_url: str | None,
) -> dict | None:
    """
    Tente d'enrichir un lead.
    1. societe.com via Playwright (plus fiable)
    2. Fallback API gouvernementale si disponible
    3. Fallback SIRET depuis site web
    Retourne None si aucun résultat, 'CAPTCHA' si bloqué.
    """
    # 1. societe.com
    result = await scraper_lead_societe(page, nom, dept)
    if result == "CAPTCHA":
        return "CAPTCHA"
    if result:
        return result

    # 2. API gouvernementale (fallback)
    if api_url:
        api_result = await chercher_entreprise_api(http, nom, dept, api_url=api_url)
        if api_result:
            return _extraire_infos_api(api_result)

    # 3. SIRET depuis site web (dernier recours)
    if website:
        siret = await extraire_siret_depuis_site(http, website)
        if siret:
            return {
                "siret": siret, "nom_gerant": None, "prenom_gerant": None,
                "forme_juridique": None, "code_naf": None, "radie": False,
                "date_creation": None, "source": "site-web",
            }

    return None


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

async def afficher_stats(conn):
    total    = await conn.fetchval("SELECT COUNT(*) FROM landscapers WHERE statut != 'a_ferme'")
    enrichis = await conn.fetchval(
        "SELECT COUNT(*) FROM landscapers WHERE nom_gerant IS NOT NULL AND statut != 'a_ferme'"
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

async def run(dept_filter: str | None, limit: int | None, dry_run: bool, delay: float):
    conn = await asyncpg.connect(_DB_URL)

    # Créer les colonnes manquantes si nécessaire
    await conn.execute("""
        ALTER TABLE landscapers
          ADD COLUMN IF NOT EXISTS prenom_gerant   TEXT,
          ADD COLUMN IF NOT EXISTS nom_gerant      TEXT,
          ADD COLUMN IF NOT EXISTS siret           TEXT,
          ADD COLUMN IF NOT EXISTS forme_juridique TEXT,
          ADD COLUMN IF NOT EXISTS date_creation   TEXT,
          ADD COLUMN IF NOT EXISTS code_naf        TEXT
    """)

    # Vérification API gouvernementale au démarrage
    api_url: str | None = None
    log.info("Vérification API gouvernementale...")
    if await _tester_api(API_URL):
        api_url = API_URL
        log.info("API gouvernementale OK (%s)", API_URL)
    elif await _tester_api(API_URL_ALT):
        api_url = API_URL_ALT
        log.info("API gouvernementale OK - fallback (%s)", API_URL_ALT)
    else:
        log.warning("API gouvernementale inaccessible — fallback désactivé")

    # Leads à enrichir (nom_gerant IS NULL)
    where = "nom_gerant IS NULL AND name IS NOT NULL AND (statut IS NULL OR statut != 'a_ferme')"
    if dept_filter:
        where += f" AND dept = '{dept_filter}'"

    rows = await conn.fetch(
        f"SELECT place_id, name, dept, address, website FROM landscapers WHERE {where} ORDER BY scraped_at DESC"
        + (f" LIMIT {limit}" if limit else "")
    )

    total = len(rows)
    if not total:
        log.info("Aucun lead à enrichir.")
        await conn.close()
        return

    prefix = "[DRY-RUN] " if dry_run else ""
    log.info("%s>> %d leads à enrichir (societe.com → API fallback)", prefix, total)

    enrichis       = 0
    exclus         = 0
    echecs         = 0
    debut          = time.time()
    fiches_session = 0

    async with async_playwright() as pw:
        browser, context, page = await creer_contexte_societe(pw)

        async with httpx.AsyncClient(headers={"User-Agent": "geo-leaad-enrichissement/1.0"}) as http:
            for idx, row in enumerate(rows, 1):
                nom     = row["name"] or ""
                pid     = row["place_id"]
                dept    = row["dept"]
                address = row["address"] if "address" in row.keys() else None
                website = row["website"] if "website" in row.keys() else None

                # Redémarrer le browser toutes les N fiches
                if fiches_session >= BROWSER_RESTART_EVERY:
                    log.info("Redémarrage du browser après %d fiches", fiches_session)
                    await fermer_browser(browser)
                    await asyncio.sleep(random.uniform(2, 5))
                    browser, context, page = await creer_contexte_societe(pw)
                    fiches_session = 0

                result = await enrichir_lead(page, http, nom, dept, website, api_url)
                fiches_session += 1

                # Gestion CAPTCHA
                if result == "CAPTCHA":
                    log.warning("CAPTCHA détecté — pause %d min", CAPTCHA_PAUSE_SECONDS // 60)
                    await fermer_browser(browser)
                    await asyncio.sleep(CAPTCHA_PAUSE_SECONDS)
                    browser, context, page = await creer_contexte_societe(pw)
                    fiches_session = 0
                    result = await enrichir_lead(page, http, nom, dept, website, api_url)
                    fiches_session += 1
                    if result == "CAPTCHA":
                        log.error("CAPTCHA persistant — abandon : %s", nom[:50])
                        echecs += 1
                        continue

                if not result:
                    echecs += 1
                else:
                    if not dry_run:
                        radie = result.get("radie", False)
                        if radie:
                            await conn.execute(
                                """UPDATE landscapers SET
                                    prenom_gerant   = $1,
                                    nom_gerant      = $2,
                                    siret           = $3,
                                    forme_juridique = $4,
                                    code_naf        = $5,
                                    statut          = 'a_ferme'
                                WHERE place_id = $6""",
                                result.get("prenom_gerant"),
                                result.get("nom_gerant"),
                                result.get("siret"),
                                result.get("forme_juridique"),
                                result.get("code_naf"),
                                pid,
                            )
                            exclus += 1
                            log.info("RADIÉ→A_FERME  %s", nom[:60])
                        else:
                            await conn.execute(
                                """UPDATE landscapers SET
                                    prenom_gerant   = $1,
                                    nom_gerant      = $2,
                                    siret           = $3,
                                    forme_juridique = $4,
                                    code_naf        = $5,
                                    date_creation   = $6
                                WHERE place_id = $7""",
                                result.get("prenom_gerant"),
                                result.get("nom_gerant"),
                                result.get("siret"),
                                result.get("forme_juridique"),
                                result.get("code_naf"),
                                result.get("date_creation"),
                                pid,
                            )
                        enrichis += 1
                        gerant = f"{result.get('prenom_gerant') or ''} {result.get('nom_gerant') or ''}".strip()
                        log.info("OK [%s]  %-50s  %s  SIRET:%s",
                                 result.get("source", "?"),
                                 nom[:50], gerant or "(gérant non trouvé)",
                                 result.get("siret") or "-")
                    else:
                        enrichis += 1

                # Progression toutes les 10 fiches
                if idx % 10 == 0 or idx == total:
                    elapsed  = time.time() - debut
                    vitesse  = round(enrichis / elapsed * 3600) if elapsed > 1 else 0
                    pct      = round(idx / total * 100)
                    bar_fill = int(pct / 5)
                    bar      = "█" * bar_fill + "░" * (20 - bar_fill)
                    print(
                        f"  [{bar}] {idx}/{total} ({pct}%)  "
                        f"enrichis:{enrichis}  exclus:{exclus}  échecs:{echecs}  ~{vitesse}/h",
                        flush=True,
                    )

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
    parser = argparse.ArgumentParser(description="Enrichissement unifié (societe.com + API gouvernementale)")
    parser.add_argument("--dept",    metavar="XX",  help="Limite à un département (ex: 06)")
    parser.add_argument("--limit",   metavar="N",   type=int, help="Nombre max de leads à traiter")
    parser.add_argument("--dry-run", action="store_true", help="Simule sans écrire en base")
    parser.add_argument("--stats",   action="store_true", help="Affiche uniquement les statistiques")
    parser.add_argument("--delay",   metavar="SEC", type=float, default=1.0,
                        help="Délai entre appels API fallback (défaut: 1s)")
    args = parser.parse_args()

    if args.stats:
        async def show():
            conn = await asyncpg.connect(_DB_URL)
            await afficher_stats(conn)
            await conn.close()
        asyncio.run(show())
        return

    asyncio.run(run(args.dept, args.limit, args.dry_run, args.delay))


if __name__ == "__main__":
    main()
