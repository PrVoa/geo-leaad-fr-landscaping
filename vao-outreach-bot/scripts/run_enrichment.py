#!/usr/bin/env python3
"""CLI : lance l'enrichissement de prospects."""

import sys
import os
import asyncio
import argparse
import logging

# Ajouter le répertoire parent au path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from enrichment.enricher import run_enrichment


def main():
    parser = argparse.ArgumentParser(description="Enrichissement des prospects VAO")
    parser.add_argument("-n", "--limit", type=int, default=100,
                        help="Nombre max de prospects à enrichir (défaut: 100)")
    parser.add_argument("--delay", type=float, default=None,
                        help="Délai en secondes entre chaque prospect (défaut: random 60-120s)")
    parser.add_argument("--fast", action="store_true",
                        help="Mode rapide: 2s entre chaque prospect (dev/test)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Logs détaillés (DEBUG)")
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    delay = 2.0 if args.fast else args.delay

    stats = asyncio.run(run_enrichment(limit=args.limit, delay_between=delay))

    if stats["total"] == 0:
        print("\nAucun prospect à enrichir.")
    else:
        print(f"\n{'─' * 50}")
        print(f"  Total traités :  {stats['total']}")
        print(f"  Scorés (tier) :  {stats['scored']}")
        print(f"  Sans formulaire: {stats['no_form']}")
        print(f"  Échecs :         {stats['failed']}")
        print(f"{'─' * 50}")


if __name__ == "__main__":
    main()
