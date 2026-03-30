"""
Scheduler grille géographique — objectif 33 500 fiches paysagistes France.

Approche : grille 0.3° × 0.3° (~25 km) sur la bbox France métropolitaine.
Les 16 000 leads existants sont analysés pour pré-marquer les cellules déjà couvertes.

Usage :
    python scheduler.py           → reprend où arrêté (mode normal)
    python scheduler.py --reset   → repart de zéro (ignore les leads existants)
    python scheduler.py --stats   → affiche les stats sans scraper
    python scheduler.py --missing → scrape uniquement les zones avec 0 leads
    python scheduler.py --dry-run → test sans écriture en base
"""
import asyncio
import argparse
import json
import random
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config
from config import log, DATABASE_URL, CAPTCHA_WAIT, HEADLESS
from models import Landscaper
from scrapers import (
    BlocageDetecte,
    accepter_cookies,
    detecter_blocage,
    scroll_jusqu_epuisement,
    _extraire_place_ids,
    scraper_fiche,
)

try:
    from playwright_stealth import Stealth
    async def _apply_stealth(page):
        await Stealth().apply_stealth_async(page)
except (ImportError, AttributeError):
    async def _apply_stealth(page):
        pass

from playwright.async_api import async_playwright
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker


# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

# Bbox France métropolitaine
LAT_MIN, LAT_MAX = 42.0, 51.5
LON_MIN, LON_MAX = -5.0, 9.5
CELL_SIZE        = 0.3        # ~25 km par côté
ZOOM             = 12

COVERAGE_THRESHOLD = 5        # leads dans la bbox → cellule considérée done
OBJECTIF_TOTAL     = 33_500

DELAY_FICHE = (1.5, 2.0)      # secondes entre fiches
DELAY_CELL  = (8.0, 12.0)     # secondes entre cellules

FILTER_KEYWORDS = [
    "velib", "belib", "parking", "borne", "metro", "supermarche",
    "carrefour", "leclerc", "maison et services", "axeo",
    "centre services", "eurovia", "esat", "lycée", "lycee",
    "camping", "daniel moquet",
]

PROGRESS_FILE = Path(__file__).resolve().parent / "grid_tasks.json"

# Coordonnées approx des centres de département (pour couverture des leads sans lat/lon)
DEPT_CENTERS: dict[str, tuple[float, float]] = {
    "01": (46.10, 5.35), "02": (49.55, 3.50), "03": (46.35, 3.40),
    "04": (44.10, 6.25), "05": (44.65, 6.35), "06": (43.95, 7.10),
    "07": (44.75, 4.50), "08": (49.80, 4.75), "09": (42.95, 1.55),
    "10": (48.30, 4.10), "11": (43.15, 2.50), "12": (44.30, 2.75),
    "13": (43.50, 5.45), "14": (49.10, -0.35), "15": (45.10, 2.70),
    "16": (45.70, 0.25), "17": (45.80, -0.75), "18": (47.10, 2.50),
    "19": (45.35, 2.00), "2A": (41.85, 9.00), "2B": (42.40, 9.25),
    "21": (47.35, 4.85), "22": (48.45, -2.80), "23": (46.00, 2.05),
    "24": (45.10, 0.75), "25": (47.25, 6.55), "26": (44.75, 5.10),
    "27": (49.10, 1.20), "28": (48.45, 1.40), "29": (48.20, -4.15),
    "30": (44.00, 4.20), "31": (43.40, 1.45), "32": (43.65, 0.65),
    "33": (44.95, -0.50), "34": (43.60, 3.55), "35": (48.10, -1.70),
    "36": (46.70, 1.65), "37": (47.25, 0.75), "38": (45.25, 5.55),
    "39": (46.70, 5.65), "40": (43.95, -0.95), "41": (47.65, 1.50),
    "42": (45.70, 4.20), "43": (45.10, 3.85), "44": (47.40, -1.55),
    "45": (47.90, 2.20), "46": (44.65, 1.65), "47": (44.35, 0.50),
    "48": (44.55, 3.55), "49": (47.50, -0.55), "50": (49.10, -1.40),
    "51": (49.05, 4.10), "52": (48.10, 5.20), "53": (48.10, -0.75),
    "54": (48.70, 6.25), "55": (48.95, 5.40), "56": (47.75, -2.75),
    "57": (49.00, 6.60), "58": (47.10, 3.50), "59": (50.45, 3.20),
    "60": (49.40, 2.50), "61": (48.60, 0.10), "62": (50.55, 2.25),
    "63": (45.80, 3.25), "64": (43.35, -0.85), "65": (43.10, 0.25),
    "66": (42.60, 2.60), "67": (48.55, 7.65), "68": (47.80, 7.30),
    "69": (45.75, 4.70), "70": (47.65, 6.15), "71": (46.60, 4.65),
    "72": (47.95, 0.25), "73": (45.55, 6.55), "74": (46.05, 6.55),
    "75": (48.87, 2.35), "76": (49.70, 1.10), "77": (48.60, 2.90),
    "78": (48.80, 1.90), "79": (46.55, -0.30), "80": (50.00, 2.30),
    "81": (43.85, 2.15), "82": (44.10, 1.35), "83": (43.50, 6.40),
    "84": (44.05, 5.10), "85": (46.70, -1.40), "86": (46.55, 0.55),
    "87": (45.85, 1.30), "88": (48.20, 6.50), "89": (47.80, 3.65),
    "90": (47.65, 6.90), "91": (48.50, 2.20), "92": (48.82, 2.22),
    "93": (48.92, 2.52), "94": (48.77, 2.50), "95": (49.10, 2.20),
}


# ---------------------------------------------------------------------------
# Grille géographique
# ---------------------------------------------------------------------------

def generate_grid() -> list[dict]:
    """Génère toutes les cellules de la grille France métropolitaine."""
    cells = []
    lat = LAT_MIN
    while round(lat, 4) < LAT_MAX:
        lon = LON_MIN
        while round(lon, 4) < LON_MAX:
            cells.append({
                "id":          f"{lat:.2f}_{lon:.2f}",
                "lat":         round(lat, 4),
                "lon":         round(lon, 4),
                "status":      "pending",
                "leads_found": 0,
            })
            lon = round(lon + CELL_SIZE, 4)
        lat = round(lat + CELL_SIZE, 4)
    log.info(f"Grille générée : {len(cells)} cellules ({CELL_SIZE}° × {CELL_SIZE}°)")
    return cells


def load_or_create_grid() -> list[dict]:
    if PROGRESS_FILE.exists():
        data = json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
        cells = data["cells"]
        s = grid_stats(cells)
        log.info(
            f"Progression chargée : {s['done']}/{s['total']} done, "
            f"{s['pending']} pending, {s['error']} erreurs"
        )
        return cells
    cells = generate_grid()
    save_grid(cells)
    return cells


def save_grid(cells: list[dict]) -> None:
    PROGRESS_FILE.write_text(
        json.dumps(
            {"cells": cells, "saved_at": datetime.now().isoformat()},
            ensure_ascii=False,
            indent=None,
        ),
        encoding="utf-8",
    )


def reset_grid() -> None:
    if PROGRESS_FILE.exists():
        PROGRESS_FILE.unlink()
        log.info("Grille supprimée — redémarrage de zéro.")


def grid_stats(cells: list[dict]) -> dict:
    total   = len(cells)
    done    = sum(1 for c in cells if c["status"] == "done")
    pending = sum(1 for c in cells if c["status"] == "pending")
    error   = sum(1 for c in cells if c["status"] == "error")
    return {"total": total, "done": done, "pending": pending, "error": error}


# ---------------------------------------------------------------------------
# Analyse de couverture des leads existants
# ---------------------------------------------------------------------------

async def mark_covered_cells(cells: list[dict], session) -> int:
    """
    Marque 'done' les cellules ayant déjà ≥ COVERAGE_THRESHOLD leads dans leur zone.

    Méthode 1 (préférée) : utilise lat/lon des leads si disponible.
    Méthode 2 (fallback)  : utilise les centres de département approximatifs.

    Retourne le nombre de cellules nouvellement marquées done.
    """
    pending = [c for c in cells if c["status"] == "pending"]
    if not pending:
        return 0

    log.info(f"Analyse couverture sur {len(pending)} cellules pending…")
    marked = 0

    # Vérifier si la colonne lat existe et si des leads ont des coordonnées
    try:
        r = await session.execute(text(
            "SELECT COUNT(*) FROM landscapers WHERE lat IS NOT NULL"
        ))
        geo_count = r.scalar() or 0
    except Exception:
        await session.rollback()
        geo_count = 0

    if geo_count > 0:
        # Méthode 1 : géographique via lat/lon
        log.info(f"  Méthode géo ({geo_count:,} leads avec coordonnées)…")
        for cell in pending:
            r = await session.execute(text("""
                SELECT COUNT(*) FROM landscapers
                WHERE lat >= :lat1 AND lat < :lat2
                  AND lon >= :lon1 AND lon < :lon2
            """), {
                "lat1": cell["lat"], "lat2": round(cell["lat"] + CELL_SIZE, 4),
                "lon1": cell["lon"], "lon2": round(cell["lon"] + CELL_SIZE, 4),
            })
            n = r.scalar() or 0
            if n >= COVERAGE_THRESHOLD:
                cell["status"] = "done"
                cell["leads_found"] = n
                marked += 1
    else:
        # Méthode 2 : approximation par département
        log.info("  Méthode dept (leads sans coordonnées, approximation par centre dept)…")
        r = await session.execute(text(
            "SELECT dept, COUNT(*) AS cnt FROM landscapers "
            "WHERE dept IS NOT NULL GROUP BY dept"
        ))
        dept_counts: dict[str, int] = {row.dept: row.cnt for row in r.fetchall()}

        for cell in pending:
            lat2 = round(cell["lat"] + CELL_SIZE, 4)
            lon2 = round(cell["lon"] + CELL_SIZE, 4)
            cell_total = 0
            for dept, (dlat, dlon) in DEPT_CENTERS.items():
                if cell["lat"] <= dlat < lat2 and cell["lon"] <= dlon < lon2:
                    cell_total += dept_counts.get(dept, 0)
            if cell_total >= COVERAGE_THRESHOLD:
                cell["status"] = "done"
                cell["leads_found"] = cell_total
                marked += 1

    log.info(
        f"Couverture : {marked} nouvelles cellules marquées done "
        f"(seuil ≥ {COVERAGE_THRESHOLD} leads)"
    )
    return marked


# ---------------------------------------------------------------------------
# Mise à jour schéma : ajout lat/lon si absent
# ---------------------------------------------------------------------------

async def ensure_lat_lon_columns(engine) -> None:
    """Ajoute les colonnes lat/lon à landscapers si elles n'existent pas encore."""
    async with engine.begin() as conn:
        for col in ("lat", "lon"):
            try:
                await conn.execute(text(
                    f"ALTER TABLE landscapers ADD COLUMN IF NOT EXISTS {col} NUMERIC(9,6)"
                ))
            except Exception as exc:
                log.debug(f"ensure_lat_lon_columns({col}) : {exc}")


# ---------------------------------------------------------------------------
# Scraping d'une cellule
# ---------------------------------------------------------------------------

async def scraper_cellule_gen(page, cell: dict, session, seen_ids: set):
    """
    Async generator : scrape une cellule de la grille.
    Navigation via coordonnées (@lat,lon,zoom).
    Réutilise les utilitaires de scrapers.py pour scroll, extraction et fiches.
    Yields 1 pour chaque lead enregistré en base.
    Lève BlocageDetecte si CAPTCHA/blocage détecté.
    """
    lat_c = round(cell["lat"] + CELL_SIZE / 2, 4)
    lon_c = round(cell["lon"] + CELL_SIZE / 2, 4)
    url = f"https://www.google.fr/maps/search/paysagiste/@{lat_c},{lon_c},{ZOOM}z"

    # Navigation avec retry ×3
    for attempt in range(3):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            await asyncio.sleep(1.5)
            break
        except Exception as exc:
            if attempt == 2:
                log.warning(f"  Cellule {cell['id']} : échec navigation après 3 essais — {exc}")
                return
            log.debug(f"  Cellule {cell['id']} navigation tentative {attempt + 1} : {exc}")
            await asyncio.sleep(5)

    await accepter_cookies(page)

    if await detecter_blocage(page):
        raise BlocageDetecte(f"Blocage sur cellule {cell['id']}")

    # Attendre au moins un résultat
    try:
        await page.wait_for_selector("a[href*='/maps/place/']", timeout=10_000)
    except Exception:
        log.info(f"  Cellule {cell['id']} → 0 résultat (zone sans paysagistes)")
        return

    await scroll_jusqu_epuisement(page)

    hrefs = await page.eval_on_selector_all(
        "a[href*='/maps/place/']", "els => els.map(e => e.href)"
    )
    pids = _extraire_place_ids(hrefs)
    log.info(f"  Cellule {cell['id']} → {len(pids)} fiche(s) trouvée(s)")

    for pid in pids:
        # Cache session — évite requête SQL répétée
        if pid in seen_ids:
            continue
        seen_ids.add(pid)

        # Filtre doublon en base
        if not config.DRY_RUN:
            existing = await session.get(Landscaper, pid)
            if existing:
                continue

        ok = await scraper_fiche(page, pid, session)
        if ok:
            # Stocker les coordonnées approx (centre cellule) si pas encore renseignées
            if not config.DRY_RUN:
                try:
                    await session.execute(text(
                        "UPDATE landscapers SET lat = :lat, lon = :lon "
                        "WHERE place_id = :pid AND lat IS NULL"
                    ), {"lat": lat_c, "lon": lon_c, "pid": pid})
                    await session.commit()
                except Exception as exc:
                    log.debug(f"  update lat/lon {pid} : {exc}")
            yield 1

        await asyncio.sleep(random.uniform(*DELAY_FICHE))


# ---------------------------------------------------------------------------
# Statistiques
# ---------------------------------------------------------------------------

async def print_stats(
    session,
    run_start: float,
    total_run: int,
    cells: list[dict] | None = None,
) -> None:
    r = await session.execute(text("SELECT COUNT(*) FROM landscapers"))
    total_db = r.scalar() or 0

    elapsed_h = (time.time() - run_start) / 3600
    speed = total_run / elapsed_h if elapsed_h > 0.001 else 0
    restant = max(0, OBJECTIF_TOTAL - total_db)

    if speed > 0:
        eta_h = restant / speed
        eta_str = str(timedelta(hours=eta_h)).split(".")[0] if eta_h < 9999 else "∞"
    else:
        eta_str = "?"

    parts = [
        f"Leads: {total_db:,}/{OBJECTIF_TOTAL:,} ({100 * total_db // OBJECTIF_TOTAL}%)",
        f"+{total_run} session",
        f"{speed:.0f}/h",
        f"ETA {eta_str}",
    ]
    if cells:
        s = grid_stats(cells)
        parts.insert(0, f"Cellules: {s['done']}/{s['total']} done ({s['pending']} pending)")

    log.info("  STATS | " + " | ".join(parts))


# ---------------------------------------------------------------------------
# Boucle principale
# ---------------------------------------------------------------------------

async def lancer_scraping(cells: list[dict], mode: str = "normal") -> int:
    """
    Scrape toutes les cellules pending.
    Reprend automatiquement là où on s'est arrêté (grid_tasks.json).
    Gère les CAPTCHAs avec pause et retry × 3.
    """
    if mode == "missing":
        targets = [c for c in cells if c["status"] == "pending" and c.get("leads_found", 0) == 0]
        log.info(f"Mode --missing : {len(targets)} cellule(s) avec 0 leads")
    else:
        targets = [c for c in cells if c["status"] == "pending"]
        log.info(f"{len(targets)} cellule(s) pending à scraper")

    if not targets:
        log.info("Aucune cellule à scraper — tout est done !")
        return 0

    engine = create_async_engine(DATABASE_URL, echo=False)
    SL = async_sessionmaker(engine, expire_on_commit=False)

    run_start  = time.time()
    total_run  = 0
    seen_ids: set[str] = set()
    cells_done_in_run   = 0
    cells_mises_de_cote: list[dict] = []

    try:
        await ensure_lat_lon_columns(engine)

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=HEADLESS,
                args=["--lang=fr-FR", "--no-sandbox"],
            )
            ctx = await browser.new_context(
                locale="fr-FR",
                timezone_id="Europe/Paris",
                viewport={"width": 1280, "height": 900},
            )
            page = await ctx.new_page()
            await _apply_stealth(page)

            for cell in targets:
                log.info(f"→ Cellule {cell['id']} (lat={cell['lat']}, lon={cell['lon']})")
                captcha_attempts = 0
                cell_count = 0

                while True:  # boucle retry CAPTCHA
                    try:
                        async with SL() as session:
                            async for _ in scraper_cellule_gen(page, cell, session, seen_ids):
                                total_run += 1
                                cell_count += 1
                                if total_run % 10 == 0:
                                    async with SL() as s2:
                                        await print_stats(s2, run_start, total_run, cells)

                        cell["status"]      = "done"
                        cell["leads_found"] = cell_count
                        if cell_count > 0:
                            log.info(f"  ✓ Cellule {cell['id']} terminée — {cell_count} lead(s)")
                        break

                    except BlocageDetecte:
                        captcha_attempts += 1
                        log.warning(
                            f"  CAPTCHA #{captcha_attempts} sur cellule {cell['id']} "
                            f"— pause {CAPTCHA_WAIT // 60} min"
                        )
                        await asyncio.sleep(CAPTCHA_WAIT)
                        if captcha_attempts >= 3:
                            log.warning(
                                f"  3 CAPTCHAs consécutifs — cellule {cell['id']} "
                                f"mise de côté pour la reprise finale"
                            )
                            cells_mises_de_cote.append(cell)
                            break

                cells_done_in_run += 1

                # Sauvegarde toutes les 10 cellules
                if cells_done_in_run % 10 == 0:
                    save_grid(cells)

                # Pause entre cellules
                await asyncio.sleep(random.uniform(*DELAY_CELL))

            # ---------------------------------------------------------------
            # Reprise des cellules mises de côté pour CAPTCHA
            # ---------------------------------------------------------------
            if cells_mises_de_cote:
                log.info("=" * 55)
                log.info(
                    f"REPRISE FINALE — {len(cells_mises_de_cote)} cellule(s) ignorée(s)"
                )
                log.info("=" * 55)

                for cell in cells_mises_de_cote:
                    async with SL() as session:
                        r = await session.execute(text("SELECT COUNT(*) FROM landscapers"))
                        if (r.scalar() or 0) >= OBJECTIF_TOTAL:
                            log.info(f"Objectif {OBJECTIF_TOTAL:,} atteint — reprise annulée.")
                            break

                    log.info(
                        f"Reprise cellule {cell['id']} — pause préventive 15 min"
                    )
                    await asyncio.sleep(CAPTCHA_WAIT)
                    cell_count = 0
                    try:
                        async with SL() as session:
                            async for _ in scraper_cellule_gen(page, cell, session, seen_ids):
                                total_run += 1
                                cell_count += 1
                        cell["status"]      = "done"
                        cell["leads_found"] = cell_count
                        log.info(
                            f"  ✓ Cellule {cell['id']} récupérée — {cell_count} lead(s)"
                        )
                    except BlocageDetecte:
                        log.error(
                            f"  Cellule {cell['id']} toujours bloquée "
                            f"— laissée pending pour la prochaine session"
                        )
                    except Exception as exc:
                        log.error(f"  Erreur reprise {cell['id']} : {exc}")

            await browser.close()

    finally:
        save_grid(cells)
        await engine.dispose()

    return total_run


# ---------------------------------------------------------------------------
# Point d'entrée
# ---------------------------------------------------------------------------

async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reset",   action="store_true",
        help="Repart de zéro en ignorant les leads existants",
    )
    parser.add_argument(
        "--stats",   action="store_true",
        help="Affiche les stats uniquement sans scraper",
    )
    parser.add_argument(
        "--missing", action="store_true",
        help="Scrape uniquement les zones avec 0 leads",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Test sans écriture en base",
    )
    args = parser.parse_args()

    if args.dry_run:
        config.DRY_RUN = True
        log.info("Mode DRY-RUN actif — pas d'écriture en base")

    if args.reset:
        reset_grid()

    cells = load_or_create_grid()

    # Créer les colonnes lat/lon si absentes, puis analyser la couverture
    engine = create_async_engine(DATABASE_URL, echo=False)
    SL = async_sessionmaker(engine, expire_on_commit=False)
    await ensure_lat_lon_columns(engine)
    if not args.reset:
        async with SL() as session:
            covered = await mark_covered_cells(cells, session)
        if covered:
            save_grid(cells)
    await engine.dispose()

    # --- Mode --stats ---
    if args.stats:
        s = grid_stats(cells)
        engine2 = create_async_engine(DATABASE_URL, echo=False)
        SL2 = async_sessionmaker(engine2, expire_on_commit=False)
        async with SL2() as session:
            r = await session.execute(text("SELECT COUNT(*) FROM landscapers"))
            total_leads = r.scalar() or 0
        await engine2.dispose()

        pct_grid  = 100 * s["done"]  // max(s["total"], 1)
        pct_leads = 100 * total_leads // OBJECTIF_TOTAL

        print("\n" + "=" * 58)
        print("  STATISTIQUES GRILLE PAYSAGISTES FRANCE")
        print("=" * 58)
        print(f"  Grille      : {s['total']} cellules (≈ {CELL_SIZE}° × {CELL_SIZE}°, ~25 km)")
        print(f"  Done        : {s['done']} cellules ({pct_grid}%)")
        print(f"  Pending     : {s['pending']} cellules")
        print(f"  Erreurs     : {s['error']} cellules")
        print(f"  Leads base  : {total_leads:,} / {OBJECTIF_TOTAL:,} ({pct_leads}%)")
        print("=" * 58 + "\n")
        return

    # --- Mode scraping ---
    log.info("=" * 55)
    log.info(f"  Scheduler Grille — Objectif {OBJECTIF_TOTAL:,} fiches")
    s = grid_stats(cells)
    log.info(
        f"  Grille : {s['total']} cellules | "
        f"{s['done']} done | {s['pending']} pending"
    )
    log.info(f"  Délais : {DELAY_FICHE[0]}-{DELAY_FICHE[1]}s/fiche | "
             f"{DELAY_CELL[0]}-{DELAY_CELL[1]}s/cellule")
    if config.DRY_RUN:
        log.info("  *** DRY-RUN — pas d'écriture en base ***")
    log.info("=" * 55)

    mode = "missing" if args.missing else "normal"
    total = await lancer_scraping(cells, mode=mode)

    log.info("=" * 55)
    log.info(f"  Session terminée : +{total} fiche(s) ajoutée(s)")
    log.info("=" * 55)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Arrêté manuellement.")
