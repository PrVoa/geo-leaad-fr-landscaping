#!/usr/bin/env python3
"""CLI : affiche la liste d'appels du jour et optionnellement l'exporte en CSV."""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tracking.call_list import generate_call_list, print_call_list, export_csv


def main():
    parser = argparse.ArgumentParser(description="Liste d'appels du jour")
    parser.add_argument("--csv", type=str, default=None,
                        help="Chemin du fichier CSV de sortie")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s — %(message)s",
        datefmt="%H:%M:%S",
    )

    prospects = generate_call_list()
    print_call_list(prospects)

    if args.csv:
        csv_content = export_csv(prospects)
        Path(args.csv).write_text(csv_content, encoding="utf-8")
        print(f"\nExporté vers {args.csv}")


if __name__ == "__main__":
    main()
