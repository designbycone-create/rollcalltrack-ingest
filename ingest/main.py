"""Entry point. Run with:
  python -m ingest.main --source all
  python -m ingest.main --source house --dry-run
  python -m ingest.main --source senate --max-filings 1
"""
from __future__ import annotations

import argparse
import logging
import sys
from contextlib import nullcontext

from . import config, db, house, senate, congress_enrich, usaspending
from .ingest_client import get_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("rollcalltrack_ingest.main")


def main():
    parser = argparse.ArgumentParser(description="rollcalltrack.com disclosure ingest")
    parser.add_argument(
        "--source",
        choices=["house", "senate", "congress_enrich", "usaspending", "all"],
        default="all",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and log but don't write (no secrets required for house/senate)",
    )
    parser.add_argument("--year", type=int, default=None)
    parser.add_argument("--days-back", type=int, default=None)
    parser.add_argument("--max-filings", type=int, default=None)
    parser.add_argument("--max-records", type=int, default=None)
    parser.add_argument("--fiscal-year", type=int, default=None)
    parser.add_argument(
        "--agency",
        default=None,
        help="USAspending agency abbreviation or name (usaspending only)",
    )
    args = parser.parse_args()

    try:
        config.require_config(dry_run=args.dry_run, source=args.source)
    except RuntimeError as e:
        logger.error(str(e))
        sys.exit(1)

    use_hz = config.use_horizons_api() and not args.dry_run
    if use_hz:
        logger.info(
            "Using Horizons API at %s (X-Ingest-Key configured)",
            config.HORIZONS_BASE_URL,
        )
    elif not args.dry_run:
        logger.info("Using legacy PocketBase direct writes")

    client = get_client() if use_hz else None

    # PocketBase conn only for legacy path or congress_enrich
    needs_pb = (
        not args.dry_run
        and not use_hz
        and args.source in ("house", "senate", "all", "congress_enrich")
    ) or (
        not args.dry_run
        and args.source == "congress_enrich"
    )

    cm = db.get_conn() if needs_pb else nullcontext(None)

    results = {}
    with cm as conn:
        if args.source in ("house", "all"):
            logger.info("=== Running House ingest ===")
            house_days = args.days_back if args.days_back is not None else 14
            max_f = args.max_filings
            if args.dry_run and max_f is None:
                max_f = 5
            results["house"] = house.run(
                conn,
                year=args.year,
                dry_run=args.dry_run,
                days_back=house_days,
                max_filings=max_f,
                client=client,
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
                client=client,
            )

        if args.source in ("congress_enrich", "all"):
            logger.info("=== Running Congress.gov enrichment ===")
            if args.dry_run and args.source == "all":
                logger.info("Skipping congress_enrich during --source all --dry-run")
            elif use_hz:
                logger.info(
                    "Skipping congress_enrich under Horizons API mode "
                    "(roster/members owned by Horizons)"
                )
            elif conn is not None:
                results["congress_enrich"] = congress_enrich.run(
                    conn, dry_run=args.dry_run
                )
            else:
                logger.warning("congress_enrich skipped — no PocketBase connection")

        if args.source == "usaspending":
            logger.info("=== Running USAspending ingest ===")
            fy = args.fiscal_year
            if fy is None:
                logger.error("--fiscal-year is required for --source usaspending")
                sys.exit(1)
            max_r = args.max_records
            if max_r is None and args.max_filings is not None:
                max_r = args.max_filings
            if args.dry_run and max_r is None:
                max_r = 5
            results["usaspending"] = usaspending.run(
                fiscal_year=fy,
                agency=args.agency,
                days_back=args.days_back,
                max_records=max_r,
                dry_run=args.dry_run,
                client=client,
            )

    logger.info("=== Run summary ===")
    for source, r in results.items():
        logger.info("%s: %s", source, r)


if __name__ == "__main__":
    main()
