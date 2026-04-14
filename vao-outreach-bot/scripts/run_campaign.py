#!/usr/bin/env python3
"""CLI : lance la campagne d'envoi du jour."""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from outreach.campaign_runner import run_campaign


def main():
    parser = argparse.ArgumentParser(description="Lance la campagne outreach du jour")
    parser.add_argument("-n", "--limit", type=int, default=None,
                        help="Nombre max de prospects à traiter (défaut: config DAILY_SEND_LIMIT)")
    parser.add_argument("--force", action="store_true",
                        help="Ignorer la vérification du jour d'envoi")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Logs DEBUG")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s — %(message)s",
        datefmt="%H:%M:%S",
    )

    report = asyncio.run(run_campaign(limit=args.limit, force=args.force))
    print()
    print(report.summary())

    # Exit code non-zéro si plus de 50% d'échecs
    if report.total_attempted > 0:
        fail_rate = report.failures / report.total_attempted
        if fail_rate > 0.5:
            sys.exit(1)


if __name__ == "__main__":
    main()
