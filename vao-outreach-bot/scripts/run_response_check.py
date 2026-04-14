#!/usr/bin/env python3
"""CLI : vérifie les réponses reçues par email (IMAP)."""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tracking.response_tracker import check_responses, run_response_loop


def main():
    parser = argparse.ArgumentParser(description="Vérifie les réponses email")
    parser.add_argument("--loop", action="store_true",
                        help="Mode boucle infinie (pour systemd)")
    parser.add_argument("--interval", type=int, default=1800,
                        help="Intervalle entre les vérifications en secondes (défaut: 1800)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s — %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.loop:
        run_response_loop(interval=args.interval)
    else:
        n = check_responses()
        print(f"{n} réponse(s) traitée(s)")


if __name__ == "__main__":
    main()
