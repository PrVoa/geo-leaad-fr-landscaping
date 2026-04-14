"""
update_departements.py

Met à jour la colonne `dept` de la table landscapers pour tous les leads
où dept IS NULL ou dept = ''.

Stratégie :
  - Extrait le code postal (5 chiffres) depuis le champ `address`
  - Convertit le code postal en code département :
      * 20xxx → Corse : 20000-20199 → 2A, 20200-20620 → 2B
      * 97xxx → DOM-TOM : conserve les 3 premiers chiffres (971..976)
      * sinon → 2 premiers chiffres du code postal

Exécution en batches de 500 avec commit par batch (pas de perte si crash)
et logs de progression.

Usage :
    python update_departements.py
    python update_departements.py --batch 500
    python update_departements.py --dry-run
    python update_departements.py --limit 1000     # max N leads (test)
"""
import argparse
import asyncio
import re
import sys

import asyncpg

from config import DB_URL as _DB_URL, get_logger

log = get_logger("update_dept")

POSTAL_RE = re.compile(r'\b(\d{5})\b')


def code_postal_to_dept(cp: str) -> str | None:
    """
    Convertit un code postal français (5 chiffres) en code département.
    Retourne None si l'entrée n'est pas un code postal valide.
    """
    if not cp or len(cp) != 5 or not cp.isdigit():
        return None

    n = int(cp)

    # DOM-TOM : 971..976 (Guadeloupe, Martinique, Guyane, Réunion, SPM, Mayotte)
    if cp.startswith("97"):
        return cp[:3]

    # Corse : 20000-20199 → 2A (Corse-du-Sud), 20200-20620 → 2B (Haute-Corse)
    if cp.startswith("20"):
        return "2A" if n <= 20199 else "2B"

    # Métropole : 2 premiers chiffres
    return cp[:2]


def extraire_dept_address(address: str | None) -> str | None:
    """Extrait le département depuis le champ address d'un lead."""
    if not address:
        return None
    m = POSTAL_RE.search(address)
    if not m:
        return None
    return code_postal_to_dept(m.group(1))


async def run(batch_size: int, dry_run: bool, limit: int | None) -> None:
    conn = await asyncpg.connect(_DB_URL)

    try:
        # Récupère tous les place_id à traiter d'un coup pour éviter
        # une boucle WHERE qui re-fetcherait les mêmes lignes non-updatables.
        query = (
            "SELECT place_id, address FROM landscapers "
            "WHERE dept IS NULL OR dept = '' "
            "ORDER BY scraped_at DESC NULLS LAST"
        )
        if limit:
            query += f" LIMIT {limit}"

        all_rows = await conn.fetch(query)
        total = len(all_rows)

        if not total:
            log.info("Aucun lead sans département. Rien à faire.")
            return

        prefix = "[DRY-RUN] " if dry_run else ""
        log.info(
            "%s%d leads sans dept à traiter (batch=%d)",
            prefix, total, batch_size,
        )

        updated = 0
        skipped = 0  # pas de code postal exploitable dans address
        processed = 0

        for start in range(0, total, batch_size):
            batch = all_rows[start:start + batch_size]
            batch_updates: list[tuple[str, str]] = []
            for row in batch:
                dept = extraire_dept_address(row["address"])
                if dept:
                    batch_updates.append((dept, row["place_id"]))
                else:
                    skipped += 1

            if batch_updates and not dry_run:
                # Commit par batch (transaction implicite via executemany dans
                # un bloc explicite pour garantir l'atomicité du batch).
                async with conn.transaction():
                    await conn.executemany(
                        "UPDATE landscapers SET dept = $1 WHERE place_id = $2",
                        batch_updates,
                    )

            updated += len(batch_updates)
            processed += len(batch)
            pct = round(processed / total * 100)
            log.info(
                "%sBatch %d-%d/%d (%d%%)  updates:%d  skipped:%d",
                prefix,
                start + 1,
                start + len(batch),
                total,
                pct,
                updated,
                skipped,
            )

        log.info(
            "%sTerminé : %d leads traités, %d updates, %d sans CP exploitable",
            prefix, processed, updated, skipped,
        )
    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Met à jour la colonne dept depuis le code postal de address.",
    )
    parser.add_argument(
        "--batch", metavar="N", type=int, default=500,
        help="Taille de batch (défaut: 500)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Affiche ce qui serait fait sans écrire en base",
    )
    parser.add_argument(
        "--limit", metavar="N", type=int, default=None,
        help="Limite le nombre total de leads traités (test)",
    )
    args = parser.parse_args()

    try:
        asyncio.run(run(args.batch, args.dry_run, args.limit))
    except KeyboardInterrupt:
        log.warning("Interruption manuelle.")
        sys.exit(130)


if __name__ == "__main__":
    main()
