"""
Scheduler haute performance — objectif 30 000 fiches paysagistes sur toute la France.

Usage :
    python scheduler.py                        # tous les départements
    python scheduler.py --dept 69              # un seul département
    python scheduler.py --dry-run              # test sans écriture DB
    python scheduler.py --headless false       # fenêtre visible
    python scheduler.py --objectif 5000        # objectif personnalisé
"""
import asyncio
import argparse
import random
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config
from config import log, DATABASE_URL, VILLES, OBJECTIF_TOTAL, CAPTCHA_WAIT
from models import Base, VilleProgress
from scrapers import BlocageDetecte, scraper_ville_gen, pause_ville

try:
    from playwright_stealth import stealth_async
except ImportError:
    from playwright_stealth import Stealth
    async def stealth_async(page):
        await Stealth().apply_stealth_async(page)

from playwright.async_api import async_playwright
from sqlalchemy import text, select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker


# ---------------------------------------------------------------------------
# Gestion de la progression (table villes_scraping)
# ---------------------------------------------------------------------------

async def init_progress(session, depts: list[str]) -> None:
    """Insère toutes les villes en 'pending' si elles ne sont pas encore trackées."""
    rows = [
        {"dept": dept, "ville": ville}
        for dept in depts
        for ville in VILLES[dept]
    ]
    await session.execute(
        text("""
            INSERT INTO villes_scraping (dept, ville, status, count)
            VALUES (:dept, :ville, 'pending', 0)
            ON CONFLICT DO NOTHING
        """),
        rows,
    )
    await session.commit()


async def get_villes_restantes(session, dept: str) -> list[str]:
    """Retourne les villes du département pas encore 'done', dans l'ordre alphabétique."""
    r = await session.execute(
        select(VilleProgress.ville)
        .where(VilleProgress.dept == dept, VilleProgress.status != "done")
        .order_by(VilleProgress.ville)
    )
    return list(r.scalars())


async def marquer_ville_done(session, dept: str, ville: str, count: int) -> None:
    prog = await session.get(VilleProgress, (dept, ville))
    if prog:
        prog.status = "done"
        prog.count = count
        prog.done_at = datetime.utcnow()
        await session.commit()


# ---------------------------------------------------------------------------
# Statistiques — affichées toutes les 10 fiches
# ---------------------------------------------------------------------------

async def afficher_stats(session, run_start: datetime, total_run: int, objectif: int) -> None:
    r = await session.execute(text("SELECT COUNT(*) FROM landscapers"))
    total_db = r.scalar() or 0

    elapsed_h = (datetime.now() - run_start).total_seconds() / 3600
    speed = total_run / elapsed_h if elapsed_h > 0.001 else 0
    restant = max(0, objectif - total_db)
    pct = total_db / objectif * 100

    if speed > 0:
        eta_h = restant / speed
        if eta_h > 24:
            eta_str = f"{eta_h / 24:.1f} jours"
        else:
            eta_str = f"{eta_h:.0f}h"
    else:
        eta_str = "?"

    log.info(
        f"  STATS | {total_db:,}/{objectif:,} ({pct:.1f}%) | "
        f"+{total_run} session | "
        f"{speed:.0f} f/h | "
        f"ETA {eta_str}"
    )


# ---------------------------------------------------------------------------
# Boucle principale
# ---------------------------------------------------------------------------

async def lancer_scraping(depts: list[str], objectif: int) -> int:
    """
    Parcourt tous les départements/villes dans l'ordre.
    Reprend automatiquement là où on s'est arrêté grâce à villes_scraping.
    Gère les CAPTCHAs avec pause et retry automatiques.
    Pas de pause entre sessions : enchaîne directement.
    """
    engine = create_async_engine(DATABASE_URL, echo=False)
    SL = async_sessionmaker(engine, expire_on_commit=False)
    run_start = datetime.now()
    total_run = 0
    villes_ignorees: list[tuple[str, str]] = []  # (dept, ville) ignorées pour CAPTCHA

    try:
        # Crée villes_scraping si elle n'existe pas (première exécution)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        # Initialise la progression pour tous les depts à scraper
        async with SL() as s:
            await init_progress(s, depts)

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=config.HEADLESS,
                args=["--lang=fr-FR", "--no-sandbox"],
            )
            ctx = await browser.new_context(
                locale="fr-FR",
                timezone_id="Europe/Paris",
                viewport={"width": 1280, "height": 900},
            )
            page = await ctx.new_page()
            await stealth_async(page)

            for dept in depts:
                # Vérifie objectif global avant chaque département
                async with SL() as s:
                    r = await s.execute(text("SELECT COUNT(*) FROM landscapers"))
                    if (r.scalar() or 0) >= objectif:
                        log.info(f"Objectif {objectif:,} atteint !")
                        break
                    villes = await get_villes_restantes(s, dept)

                if not villes:
                    log.info(f"Département {dept} déjà complet — suivant.")
                    continue

                log.info("=" * 55)
                log.info(f"DÉPARTEMENT {dept} | {len(villes)} ville(s) restante(s)")
                log.info("=" * 55)

                for i, ville in enumerate(villes):
                    # Vérifie objectif avant chaque ville
                    async with SL() as s:
                        r = await s.execute(text("SELECT COUNT(*) FROM landscapers"))
                        if (r.scalar() or 0) >= objectif:
                            log.info(f"Objectif {objectif:,} atteint !")
                            break

                    captcha_attempts = 0
                    ville_count = 0

                    while True:  # boucle retry CAPTCHA
                        try:
                            async with SL() as s:
                                async for _ in scraper_ville_gen(page, ville, s):
                                    total_run += 1
                                    ville_count += 1
                                    if total_run % 10 == 0:
                                        await afficher_stats(s, run_start, total_run, objectif)

                            if ville_count > 0:
                                async with SL() as s:
                                    await marquer_ville_done(s, dept, ville, ville_count)
                                log.info(f"  {ville} terminée — {ville_count} fiche(s)")
                            else:
                                log.warning(f"  {ville} — 0 résultat (possible blocage), laissée en pending")
                            break  # succès → passe à la ville suivante

                        except BlocageDetecte:
                            captcha_attempts += 1
                            log.warning(
                                f"  CAPTCHA #{captcha_attempts} sur {ville} "
                                f"— pause {CAPTCHA_WAIT // 60} min"
                            )
                            await asyncio.sleep(CAPTCHA_WAIT)
                            if captcha_attempts >= 3:
                                log.warning(
                                    f"  3 CAPTCHAs consécutifs — {ville} mise de côté, "
                                    f"sera retentée en fin de session"
                                )
                                villes_ignorees.append((dept, ville))
                                break

                    # Pause entre villes (pas après la dernière)
                    if i < len(villes) - 1:
                        await pause_ville()

            # -----------------------------------------------------------
            # Passe finale : retente les villes ignorées pour CAPTCHA
            # -----------------------------------------------------------
            if villes_ignorees:
                log.info("=" * 55)
                log.info(f"REPRISE FINALE — {len(villes_ignorees)} ville(s) ignorée(s)")
                log.info("=" * 55)

                for dept, ville in villes_ignorees:
                    async with SL() as s:
                        r = await s.execute(text("SELECT COUNT(*) FROM landscapers"))
                        if (r.scalar() or 0) >= objectif:
                            log.info(f"Objectif {objectif:,} atteint, reprise annulée.")
                            break

                    log.info(f"Nouvelle tentative : {ville} ({dept}) — pause préventive 15 min")
                    await asyncio.sleep(CAPTCHA_WAIT)

                    ville_count = 0
                    try:
                        async with SL() as s:
                            async for _ in scraper_ville_gen(page, ville, s):
                                total_run += 1
                                ville_count += 1
                                if total_run % 10 == 0:
                                    await afficher_stats(s, run_start, total_run, objectif)

                        if ville_count > 0:
                            async with SL() as s:
                                await marquer_ville_done(s, dept, ville, ville_count)
                            log.info(f"  {ville} récupérée — {ville_count} fiche(s)")
                        else:
                            log.warning(f"  {ville} — 0 résultat en reprise, laissée en pending")

                    except BlocageDetecte:
                        log.error(
                            f"  {ville} toujours bloquée — laissée en pending "
                            f"pour la prochaine session"
                        )
                    except Exception as exc:
                        log.error(f"  Erreur sur {ville} en reprise finale : {exc}")

            await browser.close()

    finally:
        await engine.dispose()

    return total_run


# ---------------------------------------------------------------------------
# Point d'entrée
# ---------------------------------------------------------------------------

async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scraper paysagistes Google Maps — objectif 30 000 fiches"
    )
    parser.add_argument(
        "--dept", type=str,
        help="Code département (ex: 69). Omis = tous les départements dans l'ordre"
    )
    parser.add_argument(
        "--headless", type=str, default=None, choices=["true", "false"],
        help="Override HEADLESS du .env"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Extrait les données sans écrire en base"
    )
    parser.add_argument(
        "--objectif", type=int, default=OBJECTIF_TOTAL,
        help=f"Objectif total (défaut : {OBJECTIF_TOTAL:,})"
    )
    args = parser.parse_args()

    if args.headless is not None:
        config.HEADLESS = args.headless == "true"
    if args.dry_run:
        config.DRY_RUN = True
        log.info("Mode DRY-RUN actif — pas d'écriture en base")

    if args.dept:
        dept = args.dept.zfill(2)
        if dept not in VILLES:
            log.error(f"Département {dept!r} inconnu. Disponibles : {', '.join(sorted(VILLES))}")
            sys.exit(1)
        depts = [dept]
    else:
        depts = sorted(VILLES.keys())  # tous les départements, du 01 au 95

    nb_villes = sum(len(VILLES[d]) for d in depts)
    log.info("=" * 55)
    log.info(f"  Scraper Paysagistes — Objectif {args.objectif:,} fiches")
    log.info(f"  {len(depts)} département(s) | {nb_villes} ville(s)")
    log.info(f"  Délais : {config.MIN_DELAY_FICHE}-{config.MAX_DELAY_FICHE}s/fiche | "
             f"{config.MIN_DELAY}-{config.MAX_DELAY}s/ville")
    if config.DRY_RUN:
        log.info("  *** DRY-RUN — pas d'écriture en base ***")
    log.info("=" * 55)

    total = await lancer_scraping(depts, args.objectif)

    log.info("=" * 55)
    log.info(f"  Session terminée : +{total} fiches ajoutées")
    log.info("=" * 55)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Arrêté manuellement.")
