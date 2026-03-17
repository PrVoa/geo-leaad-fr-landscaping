import asyncio
import os
import re
import random
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv()

from playwright.async_api import async_playwright
from playwright_stealth import stealth_async
from sqlalchemy import Column, DateTime, Float, Integer, Text, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

# ── Config ────────────────────────────────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL")
HEADLESS     = os.getenv("HEADLESS", "false").lower() == "true"
MIN_DELAY    = int(os.getenv("MIN_DELAY", "10"))
MAX_DELAY    = int(os.getenv("MAX_DELAY", "30"))
DAILY_LIMIT  = 50  # max paysagistes par jour

# ── Modèle ────────────────────────────────────────────────────────────────────
class Base(DeclarativeBase):
    pass

class Landscaper(Base):
    __tablename__ = "landscapers"
    place_id     = Column(Text, primary_key=True)
    name         = Column(Text, nullable=False)
    phone        = Column(Text, nullable=True)
    address      = Column(Text, nullable=True)
    website      = Column(Text, nullable=True)
    rating       = Column(Float, nullable=True)
    review_count = Column(Integer, nullable=True)
    latitude     = Column(Float, nullable=True)
    longitude    = Column(Float, nullable=True)
    maps_url     = Column(Text, nullable=True)
    scraped_at   = Column(DateTime, default=datetime.utcnow)

# ── Liste des villes par département ──────────────────────────────────────────
VILLES = {
    "01": ["Bourg-en-Bresse","Oyonnax","Ambérieu-en-Bugey"],
    "06": ["Nice","Cannes","Antibes","Menton","Grasse"],
    "13": ["Marseille","Aix-en-Provence","Arles","Martigues"],
    "21": ["Dijon","Beaune","Chenôve"],
    "25": ["Besançon","Montbéliard","Pontarlier"],
    "31": ["Toulouse","Blagnac","Colomiers","Tournefeuille"],
    "33": ["Bordeaux","Mérignac","Pessac","Talence"],
    "34": ["Montpellier","Béziers","Sète","Agde"],
    "35": ["Rennes","Saint-Malo","Fougères","Vitré"],
    "38": ["Grenoble","Vienne","Échirolles"],
    "44": ["Nantes","Saint-Nazaire","Saint-Herblain"],
    "45": ["Orléans","Gien","Montargis"],
    "49": ["Angers","Cholet","Saumur"],
    "51": ["Reims","Châlons-en-Champagne","Épernay"],
    "54": ["Nancy","Vandœuvre-lès-Nancy","Lunéville"],
    "57": ["Metz","Thionville","Forbach"],
    "59": ["Lille","Roubaix","Tourcoing","Dunkerque","Valenciennes"],
    "62": ["Calais","Boulogne-sur-Mer","Arras","Lens"],
    "63": ["Clermont-Ferrand","Riom","Issoire"],
    "67": ["Strasbourg","Haguenau","Schiltigheim"],
    "69": ["Lyon","Villeurbanne","Vénissieux","Saint-Priest","Bron","Caluire-et-Cuire"],
    "74": ["Annecy","Thonon-les-Bains","Annemasse"],
    "75": ["Paris 1er","Paris 8ème","Paris 15ème","Paris 16ème"],
    "76": ["Rouen","Le Havre","Dieppe"],
    "77": ["Melun","Meaux","Fontainebleau"],
    "78": ["Versailles","Saint-Germain-en-Laye","Mantes-la-Jolie"],
    "80": ["Amiens","Abbeville"],
    "83": ["Toulon","Fréjus","Hyères"],
    "84": ["Avignon","Orange","Carpentras"],
    "85": ["La Roche-sur-Yon","Les Sables-d'Olonne"],
    "86": ["Poitiers","Châtellerault"],
    "87": ["Limoges","Saint-Junien"],
    "91": ["Évry","Corbeil-Essonnes","Massy"],
    "92": ["Nanterre","Boulogne-Billancourt","Colombes"],
    "93": ["Saint-Denis","Montreuil","Aubervilliers"],
    "94": ["Créteil","Vincennes","Vitry-sur-Seine"],
    "95": ["Cergy","Argenteuil","Sarcelles"],
}

# ── Helpers ───────────────────────────────────────────────────────────────────
async def pause():
    t = random.uniform(MIN_DELAY, MAX_DELAY)
    print(f"  ⏳ Pause {t:.0f}s...")
    await asyncio.sleep(t)

def clean_phone(raw):
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10 and digits.startswith("0"):
        return digits
    if len(digits) == 11 and digits.startswith("33"):
        return "0" + digits[2:]
    return raw

async def get_text(page, selector):
    try:
        el = page.locator(selector).first
        if await el.is_visible(timeout=2000):
            return (await el.inner_text()).strip()
    except:
        pass
    return None

async def get_detail(page, label):
    """Extrait téléphone, adresse, site web depuis la fiche Google Maps."""
    selectors = [
        f"button[data-item-id*='{label}']",
        f"a[data-item-id*='{label}']",
        f"[aria-label*='{label}']",
    ]
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=1500):
                return (await el.inner_text()).strip()
        except:
            continue
    return None

# ── Scraping d'une fiche ──────────────────────────────────────────────────────
async def scrape_fiche(page, place_id, session):
    """Ouvre une fiche Google Maps et extrait les données."""
    url = f"https://www.google.fr/maps/place/?q=place_id:{place_id}"
    await page.goto(url, wait_until="domcontentloaded", timeout=20000)
    await asyncio.sleep(2)

    name    = await get_text(page, "h1")
    phone   = clean_phone(await get_detail(page, "phone"))
    website = await get_detail(page, "website")
    address = await get_detail(page, "address")

    # Rating
    rating, review_count = None, None
    try:
        el = await page.locator("span[aria-label*='étoile']").first.get_attribute("aria-label")
        if el:
            m = re.search(r"([\d,]+)\s*étoile", el)
            if m:
                rating = float(m.group(1).replace(",", "."))
            m2 = re.search(r"([\d\s]+)\s*avis", el)
            if m2:
                review_count = int(m2.group(1).replace(" ", ""))
    except:
        pass

    return name, phone, website, address, rating, review_count

# ── Scraping d'une ville ──────────────────────────────────────────────────────
async def scrape_ville(page, ville, session, compteur):
    """Scrape les paysagistes d'une ville. Retourne le nb de nouveaux leads."""
    query = f"paysagiste {ville}"
    url   = f"https://www.google.fr/maps/search/{query.replace(' ', '+')}"
    print(f"\n🔍 {query}")

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)

        # Accepte les cookies si présents
        try:
            btn = page.locator("button:has-text('Tout accepter')").first
            if await btn.is_visible(timeout=3000):
                await btn.click()
                await asyncio.sleep(2)
        except:
            pass

        # Scroll pour charger plus de résultats
        for _ in range(4):
            await page.keyboard.press("End")
            await asyncio.sleep(1.5)

        # Collecte les place_id
        links = await page.locator("a[href*='/maps/place/']").all()
        hrefs = list({await l.get_attribute("href") for l in links if await l.get_attribute("href")})
        place_ids = []
        for href in hrefs:
            m = re.search(r"place/[^/]+/[^@]+@[\d.]+,[\d.]+", href)
            pid_match = re.search(r"1s([^&/]+)", href)
            if pid_match:
                place_ids.append(pid_match.group(1))

        # Fallback : extrait depuis l'URL directement
        if not place_ids:
            for href in hrefs[:15]:
                m = re.search(r"/maps/place/(.+?)/@", href)
                if m:
                    place_ids.append(m.group(1))

        place_ids = list(set(place_ids))[:15]
        print(f"  📍 {len(place_ids)} fiches trouvées")

        saved = 0
        for place_id in place_ids:
            if compteur[0] >= DAILY_LIMIT:
                print(f"  🛑 Limite journalière de {DAILY_LIMIT} atteinte !")
                return saved

            # Vérifie si déjà en base
            result = await session.execute(
                text("SELECT place_id FROM landscapers WHERE place_id = :pid"),
                {"pid": place_id}
            )
            if result.fetchone():
                print(f"  ⏭️  Déjà en base : {place_id[:30]}")
                continue

            try:
                name, phone, website, address, rating, review_count = \
                    await scrape_fiche(page, place_id, session)

                if not name:
                    continue

                landscaper = Landscaper(
                    place_id=place_id,
                    name=name,
                    phone=phone,
                    address=address,
                    website=website,
                    rating=rating,
                    review_count=review_count,
                    maps_url=f"https://www.google.fr/maps/place/?q=place_id:{place_id}",
                    scraped_at=datetime.utcnow(),
                )
                session.add(landscaper)
                await session.commit()
                saved += 1
                compteur[0] += 1
                print(f"  ✅ [{compteur[0]}/{DAILY_LIMIT}] {name} | {phone or '—'} | {website or '—'}")
                await asyncio.sleep(random.uniform(2, 4))

            except Exception as e:
                print(f"  ⚠️  Erreur fiche : {e}")
                continue

        print(f"  💾 {saved} nouveaux sauvegardés pour {ville}")
        await pause()
        return saved

    except Exception as e:
        print(f"  ❌ Erreur {ville} : {e}")
        return 0

# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    dept = input("Numéro de département (ex: 69) : ").strip().zfill(2)

    if dept not in VILLES:
        print(f"❌ Département {dept} non trouvé.")
        print(f"   Disponibles : {', '.join(sorted(VILLES.keys()))}")
        sys.exit(1)

    villes = VILLES[dept]
    print(f"\n🗺️  Département {dept} — {len(villes)} villes")
    print(f"   Objectif : {DAILY_LIMIT} paysagistes max")
    print(f"   Villes   : {', '.join(villes)}\n")

    engine = create_async_engine(DATABASE_URL, echo=False)
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=HEADLESS,
            args=["--lang=fr-FR"]
        )
        context = await browser.new_context(
            locale="fr-FR",
            timezone_id="Europe/Paris",
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()
        await stealth_async(page)

        compteur = [0]  # compteur mutable partagé

        async with SessionLocal() as session:
            for i, ville in enumerate(villes, 1):
                if compteur[0] >= DAILY_LIMIT:
                    break
                print(f"\n[{i}/{len(villes)}] {ville} — {compteur[0]}/{DAILY_LIMIT} collectés")
                await scrape_ville(page, ville, session, compteur)

        await browser.close()
    await engine.dispose()

    print(f"\n🎉 Terminé ! {compteur[0]} paysagistes ajoutés en base.\n")

if __name__ == "__main__":
    asyncio.run(main())