"""Entry point. Run with:
  python -m ingest.main --source all
  python -m ingest.main --source house --dry-run
  python -m ingest.main --source senate
  python -m ingest.main --source congress_enrich
"""
from __future__ import annotations

import argparse
import logging
import sys
from contextlib import nullcontext

from . import config, db, house, senate, congress_enrich

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("rollcalltrack_ingest.main")


def main():
    parser = argparse.ArgumentParser(description="rollcalltrack.com disclosure ingest")
    parser.add_argument(
        "--source",
        choices=["house", "senate", "congress_enrich", "all"],
        default="all",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and log but don't write to the database (no DB required for house/senate)",
    )
    parser.add_argument(
        "--year",
        type=int,
        default=None,
        help="Filing year for House source (defaults to current year)",
    )
    parser.add_argument(
        "--days-back",
        type=int,
        default=None,
        help="Lookback window in days (Senate default 2, House default 14)",
    )
    parser.add_argument(
        "--max-filings",
        type=int,
        default=None,
        help="Cap number of filings to process (useful for smoke tests)",
    )
    args = parser.parse_args()

    try:
        config.require_config(dry_run=args.dry_run, source=args.source)
    except RuntimeError as e:
        logger.error(str(e))
        sys.exit(1)

    results = {}
    # Dry-run for house/senate skips DB so parse verification works without secrets.
    # congress_enrich always needs a connection (reads members table).
    needs_db = (not args.dry_run) or (args.source == "congress_enrich")
    cm = db.get_conn() if needs_db else nullcontext(None)

    with cm as conn:
        if args.source in ("house", "all"):
            logger.info("=== Running House ingest ===")
            house_days = args.days_back if args.days_back is not None else 14
            max_f = args.max_filings
            if args.dry_run and max_f is None:
                max_f = 5  # keep dry-run snappy unless user overrides
            results["house"] = house.run(
                conn,
                year=args.year,
                dry_run=args.dry_run,
                days_back=house_days,
                max_filings=max_f,
            )

        if args.source in ("senate", "all"):
            logger.info("=== Running Senate ingest ===")
            senate_days = args.days_back if args.days_back is not None else 2
            max_f = args.max_filings
            if args.dry_run and max_f is None:
                max_f = 5
            results["senate"] = senate.run(
                conn,
                days_back=senate_days,
                dry_run=args.dry_run,
                max_filings=max_f,
            )

        if args.source in ("congress_enrich", "all"):
            logger.info("=== Running Congress.gov enrichment ===")
            if args.dry_run and args.source == "all":
                logger.info("Skipping congress_enrich during --source all --dry-run")
            else:
                results["congress_enrich"] = congress_enrich.run(
                    conn, dry_run=args.dry_run
                )

    logger.info("=== Run summary ===")
    for source, r in results.items():
        logger.info("%s: %s", source, r)


if __name__ == "__main__":
    main()
