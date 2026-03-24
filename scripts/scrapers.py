"""
Fonctions de scraping Google Maps (utilisées par scheduler.py).
"""
import asyncio
import re
import random
from datetime import datetime
from urllib.parse import quote_plus

from playwright.async_api import Page
from sqlalchemy.ext.asyncio import AsyncSession

import config
from config import log, MIN_DELAY, MAX_DELAY, MIN_DELAY_FICHE, MAX_DELAY_FICHE, MOTS_EXCLUS, MOTS_CLES_RECHERCHE, CATEGORIES_EXCLUES
from models import Landscaper


# ---------------------------------------------------------------------------
# Exceptions métier
# ---------------------------------------------------------------------------

class BlocageDetecte(Exception):
    """Google a affiché une page CAPTCHA ou de blocage."""


# ---------------------------------------------------------------------------
# Utilitaires
# ---------------------------------------------------------------------------

def clean_phone(raw: str | None) -> str | None:
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10 and digits.startswith("0"):
        return digits
    if len(digits) == 11 and digits.startswith("33"):
        return "0" + digits[2:]
    # Format non reconnu → on ne stocke rien plutôt que des données corrompues
    return None


async def pause_ville() -> None:
    t = random.uniform(MIN_DELAY, MAX_DELAY)
    log.info(f"Pause {t:.0f}s avant la prochaine ville...")
    await asyncio.sleep(t)


async def pause_fiche() -> None:
    await asyncio.sleep(random.uniform(MIN_DELAY_FICHE, MAX_DELAY_FICHE))


# ---------------------------------------------------------------------------
# Interaction page
# ---------------------------------------------------------------------------

async def accepter_cookies(page: Page) -> None:
    try:
        btn = page.locator("button:has-text('Tout accepter')").first
        if await btn.is_visible(timeout=3000):
            await btn.click()
            await asyncio.sleep(2)
    except Exception:
        pass  # Pas de bandeau cookies → on continue silencieusement


async def detecter_blocage(page: Page) -> bool:
    """Retourne True si Google a détecté un bot (CAPTCHA ou page sorry)."""
    url = page.url
    if "sorry/index" in url or "sorry?continue" in url:
        return True
    try:
        title = await page.title()
        if any(kw in title.lower() for kw in ("unusual traffic", "trafic inhabituel")):
            return True
        if await page.locator("#captcha-form, #recaptcha, iframe[src*='recaptcha']").count() > 0:
            return True
    except Exception:
        pass
    return False


async def extraire_categorie(page: Page) -> str | None:
    """Extrait la catégorie affichée sous le nom sur Google Maps."""
    for sel in [
        "button[jsaction*='category']",
        "span[jstcache] > button",
        "div[role='main'] button[aria-label]",
    ]:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=1500):
                t = (await el.inner_text()).strip()
                if t and len(t) < 60:
                    return t.lower()
        except Exception:
            continue
    return None


async def extraire_texte(page: Page, selector: str) -> str | None:
    try:
        el = page.locator(selector).first
        if await el.is_visible(timeout=2000):
            return (await el.inner_text()).strip()
    except Exception as exc:
        log.debug(f"extraire_texte({selector!r}) : {exc}")
    return None


async def extraire_champ(page: Page, labels: list[str]) -> str | None:
    for label in labels:
        for sel in [
            f"button[data-item-id*='{label}']",
            f"a[data-item-id*='{label}']",
            f"[aria-label*='{label}']",
        ]:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=1500):
                    t = (await el.inner_text()).strip()
                    if t:
                        return t
            except Exception:
                continue
    return None


async def extraire_website(page: Page) -> str | None:
    """Extrait l'URL réelle du site (href) et non le texte visible."""
    for sel in [
        "a[data-item-id='authority']",
        "a[aria-label*='ite Web']",
        "a[aria-label*='ite web']",
        "a[data-item-id*='website']",
        "a[data-item-id*='authority']",
    ]:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=1500):
                href = await el.get_attribute("href")
                if href and href.startswith("http") and "google" not in href:
                    return href.split("?")[0]  # supprime params de tracking
        except Exception as exc:
            log.debug(f"extraire_website({sel!r}) : {exc}")
    return None


# ---------------------------------------------------------------------------
# Extraction d'email depuis le site web
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

# Domaines techniques/réseaux sociaux à exclure (faux positifs courants)
_DOMAINES_EXCLUS = {
    "example.com", "sentry.io", "jquery.com", "google.com", "google.fr",
    "facebook.com", "instagram.com", "twitter.com", "linkedin.com",
    "wixpress.com", "squarespace.com", "wordpress.com", "mailchimp.com",
    "w3.org", "schema.org", "ogp.me", "cloudflare.com",
}


def _valider_email(raw: str) -> str | None:
    """Retourne l'email normalisé s'il est valide, None sinon."""
    email = raw.lower().strip().rstrip(".")
    if not _EMAIL_RE.fullmatch(email):
        return None
    domain = email.split("@")[1]
    if domain in _DOMAINES_EXCLUS:
        return None
    tld = domain.rsplit(".", 1)[-1]
    if len(tld) > 6:  # extension trop longue → probablement du code
        return None
    return email


async def _chercher_emails_sur_page(page: Page) -> str | None:
    """Cherche un email sur la page courante : d'abord les liens mailto, puis regex HTML."""
    # Méthode 1 : liens mailto (les plus fiables)
    try:
        mailtos = await page.eval_on_selector_all(
            "a[href^='mailto:']",
            "els => els.map(e => e.href.replace('mailto:', '').split('?')[0].trim())",
        )
        for raw in mailtos:
            email = _valider_email(raw)
            if email:
                return email
    except Exception as exc:
        log.debug(f"  _chercher_emails_sur_page mailto : {exc}")

    # Méthode 2 : regex sur le HTML complet (emails en clair ou dans du texte)
    try:
        content = await page.content()
        for raw in _EMAIL_RE.findall(content):
            email = _valider_email(raw)
            if email:
                return email
    except Exception as exc:
        log.debug(f"  _chercher_emails_sur_page regex : {exc}")

    return None


async def extraire_email(page: Page, website: str) -> str | None:
    """Visite le site web et extrait l'email de contact."""
    if not website:
        return None

    base = website.rstrip("/")
    pages_a_tester = [website] + [
        base + slug for slug in (
            "/contact", "/nous-contacter", "/contactez-nous",
            "/contact-us", "/contact.html", "/nous-contacter.html",
        )
    ]

    try:
        for i, url in enumerate(pages_a_tester):
            try:
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=8000)
                if resp and resp.status >= 400:
                    continue
                await asyncio.sleep(0.3)
                email = await _chercher_emails_sur_page(page)
                if email:
                    log.debug(f"  Email trouvé sur {url} : {email}")
                    return email
                if i == 0 and not any(kw in (await page.content()).lower() for kw in ("contact", "mail", "@")):
                    log.debug(f"  Pas d'indice d'email sur {url}, pages contact ignorées")
                    break
            except Exception as exc:
                log.debug(f"  extraire_email({url!r}) : {exc}")
                continue
    except Exception as exc:
        log.debug(f"  extraire_email global ({website!r}) : {exc}")

    return None


async def extraire_rating(page: Page) -> tuple[float | None, int | None]:
    """Extrait note (ex: 4.5) et nombre d'avis via 3 méthodes en cascade."""
    rating: float | None = None
    review_count: int | None = None

    # Méthode 1 : aria-label sur l'élément étoiles
    for sel in [
        "span[aria-label*='étoile']",
        "span[aria-label*='toile']",
        "div[aria-label*='étoile']",
        "g-review-stars[aria-label]",
    ]:
        try:
            aria = await page.locator(sel).first.get_attribute("aria-label", timeout=2000)
            if aria:
                m = re.search(r"(\d[,\.]\d)", aria)
                if m:
                    rating = float(m.group(1).replace(",", "."))
                m2 = re.search(r"(\d[\d\s\u202f]*)\s*avis", aria)
                if m2:
                    review_count = int(re.sub(r"\D", "", m2.group(1)))
                if rating is not None:
                    break
        except Exception as exc:
            log.debug(f"extraire_rating méthode 1 ({sel!r}) : {exc}")

    # Méthode 2 : grand chiffre affiché (fontDisplayLarge)
    if rating is None:
        try:
            txt = (await page.locator("div.fontDisplayLarge").first.inner_text(timeout=2000)).strip()
            candidate = float(txt.replace(",", "."))
            if 1.0 <= candidate <= 5.0:
                rating = candidate
        except Exception as exc:
            log.debug(f"extraire_rating méthode 2 : {exc}")

    # Méthode 3 : bouton "X avis" séparément
    if review_count is None:
        for sel in [
            "button[jsaction*='pane.rating.moreReviews']",
            "button[jsaction*='ReviewChart']",
            "span[aria-label*='avis']",
        ]:
            try:
                txt = (await page.locator(sel).first.inner_text(timeout=1500)).strip()
                m = re.search(r"(\d[\d\s\u202f]*)", txt)
                if m:
                    review_count = int(re.sub(r"\D", "", m.group(1)))
                    break
            except Exception as exc:
                log.debug(f"extraire_rating méthode 3 ({sel!r}) : {exc}")

    return rating, review_count


# ---------------------------------------------------------------------------
# Scraping d'une fiche individuelle
# ---------------------------------------------------------------------------

async def _scraper_fiche_once(page: Page, place_id: str, session: AsyncSession) -> bool:
    """Tente une fois. Retourne True si enregistré. Lève sur erreur."""
    await page.goto(
        f"https://www.google.fr/maps/place/?q=place_id:{place_id}",
        wait_until="domcontentloaded",
        timeout=25000,
    )
    await asyncio.sleep(random.uniform(0.8, 1.5))

    if await detecter_blocage(page):
        raise BlocageDetecte(f"Blocage sur place_id={place_id}")

    # Nom
    name: str | None = None
    try:
        await page.wait_for_selector("h1", timeout=5000)
        name = await extraire_texte(page, "h1")
    except Exception:
        pass
    if not name:
        title = await page.title()
        if title:
            name = title.split("·")[0].strip()
    if not name:
        log.debug(f"Nom introuvable pour {place_id}")
        return False
    if any(mot in name.lower() for mot in MOTS_EXCLUS):
        log.info(f"  Ignoré (hors-sujet) : {name}")
        return False

    # Filtre sur la catégorie Google Maps
    # Règle : si la catégorie contient "paysagiste" → toujours garder
    # Sinon → exclure si elle correspond à une catégorie hors-cible
    categorie = await extraire_categorie(page)
    if categorie and "paysagiste" not in categorie:
        if any(c in categorie for c in CATEGORIES_EXCLUES):
            log.info(f"  Ignoré (catégorie '{categorie}') : {name}")
            return False

    phone   = clean_phone(await extraire_champ(page, ["phone", "telephone"]))
    website = await extraire_website(page)
    address = await extraire_champ(page, ["address", "adresse"])
    rating, review_count = await extraire_rating(page)

    # Visite le site web pour chercher un email
    email = await extraire_email(page, website)

    if not config.DRY_RUN:
        obj = Landscaper(
            place_id=place_id,
            name=name,
            phone=phone,
            address=address,
            website=website,
            email=email,
            rating=rating,
            review_count=review_count,
            maps_url=f"https://www.google.fr/maps/place/?q=place_id:{place_id}",
            scraped_at=datetime.utcnow(),
            categorie=categorie,
        )
        session.add(obj)
        await session.commit()

    log.info(
        f"  {'[DRY] ' if config.DRY_RUN else ''}OK {name} | "
        f"{phone or '-'} | "
        f"{email or '-'} | "
        f"★{rating or '-'} ({review_count or 0} avis) | "
        f"{website or '-'}"
    )
    return True


async def scraper_fiche(
    page: Page,
    place_id: str,
    session: AsyncSession,
    max_retries: int = 2,
) -> bool:
    """Scrape une fiche avec retry automatique sur erreur réseau/timeout."""
    for attempt in range(max_retries + 1):
        try:
            return await _scraper_fiche_once(page, place_id, session)
        except BlocageDetecte:
            raise  # Ne jamais retenter si Google a bloqué
        except Exception as exc:
            try:
                await session.rollback()  # remet la session dans un état propre
            except Exception:
                pass
            if attempt < max_retries:
                wait = 5 * (attempt + 1)
                log.warning(f"  Retry {attempt + 1}/{max_retries} pour {place_id} (dans {wait}s) : {exc}")
                await asyncio.sleep(wait)
            else:
                log.error(f"  Echec définitif {place_id} après {max_retries + 1} tentatives : {exc}")
    return False


# ---------------------------------------------------------------------------
# Scroll de la liste jusqu'à épuisement
# ---------------------------------------------------------------------------

async def scroll_jusqu_epuisement(page: Page) -> None:
    """Scrolle le panneau latéral jusqu'à épuisement de la liste."""
    stable_rounds = 0
    last_count = 0

    for attempt in range(40):
        try:
            panneau = page.locator("div[role='feed']").first
            await panneau.evaluate("el => el.scrollTop = el.scrollHeight")
        except Exception:
            try:
                await page.keyboard.press("End")
            except Exception:
                pass

        await asyncio.sleep(1.5)

        # Fin de liste explicite
        try:
            fin = await page.locator(
                "span:has-text('Fin de la liste'), span:has-text(\"You've reached the end\")"
            ).count()
            if fin > 0:
                log.debug(f"  'Fin de la liste' détecté ({attempt + 1} scrolls)")
                break
        except Exception:
            pass

        hrefs = await page.eval_on_selector_all(
            "a[href*='/maps/place/']", "els => els.map(e => e.href)"
        )
        current = len(hrefs)
        if current == last_count:
            stable_rounds += 1
            if stable_rounds >= 3:
                log.debug(f"  Liste stable après {attempt + 1} scrolls ({current} résultats)")
                break
        else:
            stable_rounds = 0
            last_count = current


def _extraire_place_ids(hrefs: list[str]) -> list[str]:
    """Extrait et déduplique les place_id ChIJ depuis les hrefs Google Maps."""
    pids: list[str] = []
    seen: set[str] = set()
    for href in hrefs:
        if not href:
            continue
        pid: str | None = None
        m = re.search(r"!19s(ChIJ[^?&!]+)", href)
        if m:
            pid = m.group(1)
        else:
            m = re.search(r"!1s(ChIJ[^!]+)!", href)
            if m:
                pid = m.group(1)
        if pid and pid not in seen:
            seen.add(pid)
            pids.append(pid)
    return pids


# ---------------------------------------------------------------------------
# Scraping d'une ville entière
# ---------------------------------------------------------------------------

async def scraper_ville_gen(
    page: Page,
    ville: str,
    session: AsyncSession,
):
    """
    Async generator : scrape une ville avec tous les termes de MOTS_CLES_RECHERCHE.
    Déduplique les place_ids entre les recherches.
    Yields 1 pour chaque fiche enregistrée avec succès.
    Lève BlocageDetecte si Google bloque (le scheduler gère le retry).
    """
    all_pids: list[str] = []
    seen_pids: set[str] = set()

    for j, mot_cle in enumerate(MOTS_CLES_RECHERCHE):
        log.info(f"Recherche : {mot_cle} {ville}")
        try:
            await page.goto(
                f"https://www.google.fr/maps/search/{quote_plus(mot_cle)}+{quote_plus(ville)}",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            await asyncio.sleep(1.5)
            if j == 0:
                await accepter_cookies(page)

            if await detecter_blocage(page):
                raise BlocageDetecte(f"Blocage sur la recherche {ville!r}")

            try:
                await page.wait_for_selector("a[href*='/maps/place/']", timeout=10000)
            except Exception:
                log.warning(f"  Aucun résultat pour '{mot_cle} {ville}'")
                continue

            await scroll_jusqu_epuisement(page)

            hrefs = await page.eval_on_selector_all(
                "a[href*='/maps/place/']", "els => els.map(e => e.href)"
            )
            pids = _extraire_place_ids(hrefs)
            new_pids = [p for p in pids if p not in seen_pids]
            seen_pids.update(new_pids)
            all_pids.extend(new_pids)
            log.info(
                f"  {len(new_pids)} nouvelles fiches "
                f"({len(pids) - len(new_pids)} doublons) pour '{mot_cle} {ville}'"
            )

            # Pause entre les termes (plus courte qu'entre villes)
            if j < len(MOTS_CLES_RECHERCHE) - 1:
                await asyncio.sleep(random.uniform(3, 5))

        except BlocageDetecte:
            raise
        except Exception as exc:
            log.error(f"  Erreur sur '{mot_cle} {ville}' : {exc}", exc_info=True)
            continue

    log.info(f"  Total : {len(all_pids)} fiches uniques pour {ville}")

    try:
        for pid in all_pids:
            if not config.DRY_RUN:
                existing = await session.get(Landscaper, pid)
                if existing:
                    log.debug(f"  Déjà en base, skip : {pid}")
                    continue
            if await scraper_fiche(page, pid, session):
                yield 1
            await pause_fiche()

    except BlocageDetecte:
        raise
    except Exception as exc:
        log.error(f"  Erreur inattendue sur {ville} : {exc}", exc_info=True)
        return
