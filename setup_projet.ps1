# ============================================================
#  setup_projet.ps1
#  Lance ce script UNE SEULE FOIS dans ton dossier projet.
#  Il cree tous les fichiers manquants puis fait le commit.
# ============================================================

Write-Host ""
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host "  Setup automatique geo-leaad-fr-landscaping" -ForegroundColor Cyan
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host ""

# ── scheduler.py ─────────────────────────────────────────────
Write-Host "Creation de scripts\scheduler.py..." -ForegroundColor Yellow
$scheduler = @'
import asyncio, os, sys, re, random, argparse
from datetime import datetime, date
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
load_dotenv()
from playwright.async_api import async_playwright
from playwright_stealth import stealth_async
from sqlalchemy import Column, DateTime, Float, Integer, Text, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

DATABASE_URL    = os.getenv("DATABASE_URL")
HEADLESS        = os.getenv("HEADLESS", "true").lower() == "true"
MIN_DELAY       = int(os.getenv("MIN_DELAY", "10"))
MAX_DELAY       = int(os.getenv("MAX_DELAY", "30"))
OBJECTIF_JOUR   = 50
SESSIONS_PAR_JOUR = 5
PAR_SESSION     = OBJECTIF_JOUR // SESSIONS_PAR_JOUR

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

class GridTask(Base):
    __tablename__ = "grid_tasks"
    id            = Column(Integer, primary_key=True, autoincrement=True)
    min_lat       = Column(Float, nullable=False)
    min_lon       = Column(Float, nullable=False)
    max_lat       = Column(Float, nullable=False)
    max_lon       = Column(Float, nullable=False)
    status        = Column(Text, default="pending")
    results_count = Column(Integer, default=0)

VILLES = {
    "01": ["Bourg-en-Bresse","Oyonnax","Amberieu-en-Bugey"],
    "06": ["Nice","Cannes","Antibes","Menton","Grasse"],
    "13": ["Marseille","Aix-en-Provence","Arles","Martigues"],
    "21": ["Dijon","Beaune","Chenove"],
    "25": ["Besancon","Montbeliard","Pontarlier"],
    "31": ["Toulouse","Blagnac","Colomiers","Tournefeuille"],
    "33": ["Bordeaux","Merignac","Pessac","Talence","Libourne"],
    "34": ["Montpellier","Beziers","Sete","Agde"],
    "35": ["Rennes","Saint-Malo","Fougeres","Cesson-Sevigne"],
    "38": ["Grenoble","Vienne","Echirolles","Bourgoin-Jallieu"],
    "44": ["Nantes","Saint-Nazaire","Saint-Herblain","Reze"],
    "45": ["Orleans","Montargis","Olivet"],
    "49": ["Angers","Cholet","Saumur"],
    "51": ["Reims","Chalons-en-Champagne","Epernay"],
    "54": ["Nancy","Vandoeuvre-les-Nancy","Luneville"],
    "57": ["Metz","Thionville","Forbach"],
    "59": ["Lille","Roubaix","Tourcoing","Dunkerque","Valenciennes"],
    "62": ["Calais","Boulogne-sur-Mer","Arras","Lens"],
    "63": ["Clermont-Ferrand","Riom","Issoire","Vichy"],
    "67": ["Strasbourg","Haguenau","Schiltigheim"],
    "69": ["Lyon","Villeurbanne","Venissieux","Saint-Priest","Bron","Caluire-et-Cuire"],
    "74": ["Annecy","Thonon-les-Bains","Annemasse","Cluses"],
    "75": ["Paris 1er","Paris 8eme","Paris 15eme","Paris 16eme"],
    "76": ["Rouen","Le Havre","Dieppe"],
    "77": ["Melun","Meaux","Fontainebleau","Chelles"],
    "78": ["Versailles","Saint-Germain-en-Laye","Mantes-la-Jolie"],
    "80": ["Amiens","Abbeville"],
    "83": ["Toulon","Frejus","Hyeres","Draguignan"],
    "84": ["Avignon","Orange","Carpentras"],
    "85": ["La Roche-sur-Yon","Les Sables-d-Olonne","Challans"],
    "91": ["Evry","Corbeil-Essonnes","Massy","Palaiseau"],
    "92": ["Nanterre","Boulogne-Billancourt","Colombes","Asnieres-sur-Seine"],
    "93": ["Saint-Denis","Montreuil","Aubervilliers","Bobigny"],
    "94": ["Creteil","Vincennes","Vitry-sur-Seine","Ivry-sur-Seine"],
    "95": ["Cergy","Argenteuil","Sarcelles","Pontoise"],
}

def clean_phone(raw):
    if not raw: return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10 and digits.startswith("0"): return digits
    if len(digits) == 11 and digits.startswith("33"): return "0" + digits[2:]
    return raw

async def pause():
    t = random.uniform(MIN_DELAY, MAX_DELAY)
    print(f"  Pause {t:.0f}s...")
    await asyncio.sleep(t)

async def accepter_cookies(page):
    try:
        btn = page.locator("button:has-text('Tout accepter')").first
        if await btn.is_visible(timeout=3000):
            await btn.click()
            await asyncio.sleep(2)
    except: pass

async def extraire_texte(page, selector):
    try:
        el = page.locator(selector).first
        if await el.is_visible(timeout=2000):
            return (await el.inner_text()).strip()
    except: pass
    return None

async def extraire_champ(page, labels):
    for label in labels:
        for sel in [f"button[data-item-id*='{label}']", f"a[data-item-id*='{label}']", f"[aria-label*='{label}']"]:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=1500):
                    t = (await el.inner_text()).strip()
                    if t: return t
            except: continue
    return None

async def scraper_fiche(page, place_id, session):
    await page.goto(f"https://www.google.fr/maps/place/?q=place_id:{place_id}", wait_until="domcontentloaded", timeout=25000)
    await asyncio.sleep(random.uniform(2, 3))
    name    = await extraire_texte(page, "h1")
    phone   = clean_phone(await extraire_champ(page, ["phone","telephone","Telephone"]))
    website = await extraire_champ(page, ["website","site","Site Web","Website"])
    address = await extraire_champ(page, ["address","adresse","Adresse"])
    rating, review_count = None, None
    try:
        aria = await page.locator("span[aria-label*='toile']").first.get_attribute("aria-label")
        if aria:
            m = re.search(r"([\d,]+)\s*", aria)
            if m: rating = float(m.group(1).replace(",","."))
            m2 = re.search(r"([\d\s]+)\s*avis", aria)
            if m2: review_count = int(m2.group(1).replace(" ",""))
    except: pass
    if not name: return False
    try:
        await session.execute(text("""
            INSERT INTO landscapers (place_id,name,phone,address,website,rating,review_count,maps_url,scraped_at)
            VALUES (:pid,:name,:phone,:address,:website,:rating,:reviews,:url,:scraped)
            ON CONFLICT (place_id) DO NOTHING
        """), {"pid":place_id,"name":name,"phone":phone,"address":address,"website":website,
               "rating":rating,"reviews":review_count,
               "url":f"https://www.google.fr/maps/place/?q=place_id:{place_id}","scraped":datetime.utcnow()})
        await session.commit()
        print(f"  OK {name} | {phone or '-'} | {website or '-'}")
        return True
    except Exception as e:
        print(f"  Erreur : {e}")
        return False

async def scraper_ville(page, ville, session, max_r):
    print(f"\n  Recherche : paysagiste {ville}")
    try:
        await page.goto(f"https://www.google.fr/maps/search/paysagiste+{ville.replace(' ','+')}", wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)
        await accepter_cookies(page)
        for _ in range(4):
            await page.keyboard.press("End")
            await asyncio.sleep(1.5)
        links = await page.locator("a[href*='/maps/place/']").all()
        pids, seen = [], set()
        for l in links:
            href = await l.get_attribute("href")
            if not href: continue
            m = re.search(r"place/[^/]+/([^/]+)", href) or re.search(r"!1s([^!]+)!", href)
            if m and m.group(1) not in seen:
                seen.add(m.group(1)); pids.append(m.group(1))
        print(f"  {len(pids)} fiches trouvees")
        saved = 0
        for pid in pids[:max_r]:
            r = await session.execute(text("SELECT place_id FROM landscapers WHERE place_id=:pid"), {"pid":pid})
            if r.fetchone(): continue
            if await scraper_fiche(page, pid, session): saved += 1
            if saved >= max_r: break
            await asyncio.sleep(random.uniform(2,3))
        return saved
    except Exception as e:
        print(f"  Erreur {ville}: {e}"); return 0

async def lancer_session(dept, objectif, num, total):
    villes = VILLES[dept].copy()
    random.shuffle(villes)
    engine = create_async_engine(DATABASE_URL, echo=False)
    SL = async_sessionmaker(engine, expire_on_commit=False)
    total_s = 0
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS, args=["--lang=fr-FR","--no-sandbox"])
        ctx = await browser.new_context(locale="fr-FR", timezone_id="Europe/Paris", viewport={"width":1280,"height":900})
        page = await ctx.new_page()
        await stealth_async(page)
        async with SL() as session:
            r = await session.execute(text("SELECT COUNT(*) FROM landscapers WHERE DATE(scraped_at)=:d"), {"d":date.today()})
            print(f"\n  Aujourd'hui : {r.scalar()} | Objectif session : {objectif}")
            par_ville = max(2, objectif // len(villes) + 1)
            for ville in villes:
                if total_s >= objectif: break
                saved = await scraper_ville(page, ville, session, min(par_ville, objectif-total_s))
                total_s += saved
                print(f"  Session {num}/{total} : {total_s}/{objectif}")
                if total_s < objectif: await pause()
        await browser.close()
    await engine.dispose()
    return total_s

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dept", type=str, help="Numero de departement ex: 69")
    args = parser.parse_args()
    print("\n" + "="*50)
    print("  Scheduler Paysagistes - 50/jour automatique")
    print("="*50)
    dept = args.dept.zfill(2) if args.dept else input("\nDepartement (ex: 69) : ").strip().zfill(2)
    if dept not in VILLES:
        print(f"Departement {dept} non disponible. Disponibles : {', '.join(sorted(VILLES.keys()))}")
        sys.exit(1)
    print(f"\nObjectif : {OBJECTIF_JOUR}/jour en {SESSIONS_PAR_JOUR} sessions de {PAR_SESSION}")
    total_jour = 0
    for i in range(1, SESSIONS_PAR_JOUR + 1):
        print(f"\n{'─'*50}")
        print(f"  SESSION {i}/{SESSIONS_PAR_JOUR} - {datetime.now().strftime('%H:%M:%S')}")
        print(f"{'─'*50}")
        saved = await lancer_session(dept, PAR_SESSION, i, SESSIONS_PAR_JOUR)
        total_jour += saved
        print(f"\n  Session {i} terminee : +{saved} | Total jour : {total_jour}/{OBJECTIF_JOUR}")
        if i < SESSIONS_PAR_JOUR:
            print(f"\n  Prochaine session dans 2h30. En attente... (Ctrl+C pour arreter)")
            await asyncio.sleep(2.5 * 3600)
    print(f"\n{'='*50}\n  Journee terminee ! {total_jour} paysagistes ajoutes.\n{'='*50}\n")

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: print("\n  Arrete manuellement.\n")
'@
Set-Content -Path "scripts\scheduler.py" -Value $scheduler -Encoding UTF8
Write-Host "  OK" -ForegroundColor Green

# ── README.md ─────────────────────────────────────────────────
Write-Host "Mise a jour de README.md..." -ForegroundColor Yellow
$readme = @'
# geo-leaad-fr-landscaping

Scraper automatique Google Maps pour extraire des leads paysagistes en France.
Construit avec Playwright + SQLAlchemy async + PostgreSQL (Supabase).

## Structure

```
scripts/
  init_db.py          <- Cree les tables (a lancer 1 fois)
  test_connection.py  <- Teste la connexion PostgreSQL
  scraper.py          <- Scraper manuel par departement
  scheduler.py        <- 50 paysagistes/jour automatique
requirements.txt
.env.example
```

## Installation Windows (PowerShell)

```powershell
# 0. Debloquer PowerShell (si besoin)
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass

# 1. Creer et activer le venv
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2. Installer les packages
pip install -r requirements.txt

# 3. Installer Chromium
python -m playwright install --with-deps chromium

# 4. Creer le .env
copy .env.example .env
notepad .env   # remplir DATABASE_URL avec Supabase

# 5. Tester la connexion
python scripts/test_connection.py

# 6. Creer les tables
python scripts/init_db.py

# 7. Lancer le scraper
python scripts/scheduler.py --dept 69
```

## Utilisation

```powershell
# Test de connexion
python scripts/test_connection.py

# Initialiser la base (1 seule fois)
python scripts/init_db.py

# Scraper manuel (interactif)
python scripts/scraper.py

# Scheduler automatique 50/jour
python scripts/scheduler.py --dept 69
```

## Base de donnees (Supabase)

Tables creees par init_db.py :
- landscapers  : paysagistes extraits (place_id unique)
- grid_tasks   : zones geographiques a scraper
'@
Set-Content -Path "README.md" -Value $readme -Encoding UTF8
Write-Host "  OK" -ForegroundColor Green

# ── .env.example ──────────────────────────────────────────────
Write-Host "Mise a jour de .env.example..." -ForegroundColor Yellow
$envexample = @'
DATABASE_URL=postgresql+asyncpg://postgres.XXXX:MOTDEPASSE@aws-0-eu-west-1.pooler.supabase.com:5432/postgres
SEARCH_TERM=paysagistes
HEADLESS=false
MIN_DELAY=10
MAX_DELAY=30
LOG_LEVEL=INFO
'@
Set-Content -Path ".env.example" -Value $envexample -Encoding UTF8
Write-Host "  OK" -ForegroundColor Green

# ── Git commit ────────────────────────────────────────────────
Write-Host ""
Write-Host "Commit et push vers GitHub..." -ForegroundColor Yellow
git add .
git commit -m "Setup complet : scheduler.py, README, .env.example"
git push
Write-Host "  OK" -ForegroundColor Green

Write-Host ""
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host "  Tout est pret !" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Ce soir depuis ton Wi-Fi maison :" -ForegroundColor White
Write-Host "  1. python scripts/test_connection.py" -ForegroundColor White
Write-Host "  2. python scripts/init_db.py" -ForegroundColor White
Write-Host "  3. python scripts/scheduler.py --dept 69" -ForegroundColor White
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host ""
