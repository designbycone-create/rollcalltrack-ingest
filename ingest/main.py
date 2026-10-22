"""Entry point. Run with:
  python -m ingest.main --source all
  python -m ingest.main --source house --dry-run
  python -m ingest.main --source senate --max-filings 1
  python -m ingest.main --source house --filing-id 20035588
  python -m ingest.main --source house --live
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from contextlib import nullcontext

from . import (
    config,
    house,
    senate,
    usaspending,
    gao,
    oversight,
    prac,
    fjc,
    jefs,
    nominations,
    legiscan,
    legiscan_bills,
    ca_spending,
    state_spending,
    senate_votes,
)
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
        choices=[
            "house",
            "senate",
            "congress_enrich",
            "usaspending",
            "gao",
            "oversight",
            "prac",
            "fjc",
            "jefs",
            "nominations",
            "legiscan",
            "legiscan_bills",
            "ca_spending",
            "state_spending",
            "senate_votes",
            "all",
        ],
        default="all",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and log but don't write (no secrets required for house/senate)",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help=(
            "Post filings and transactions to the configured endpoint. "
            "Required unless INGEST_LIVE=1. Tests cannot use this to reach production."
        ),
    )
    parser.add_argument(
        "--full-refresh",
        action="store_true",
        help="Full directory refresh (fjc; this job has no incremental mode)",
    )
    parser.add_argument(
        "--judge-id",
        default=None,
        help="FJC nid (jefs test pull for one judge)",
    )
    parser.add_argument("--year", type=int, default=None)
    parser.add_argument("--days-back", type=int, default=None)
    parser.add_argument("--max-filings", type=int, default=None)
    parser.add_argument(
        "--filing-id",
        default=None,
        help=(
            "Re-ingest one House filing by DocID (for example 20035588). "
            "Ignores --days-back and --max-filings. Use with --source house. "
            "Pass --year when that DocID is not in the current calendar year."
        ),
    )
    parser.add_argument("--max-records", type=int, default=None)
    parser.add_argument("--fiscal-year", type=int, default=None)
    parser.add_argument(
        "--congress",
        type=int,
        default=None,
        help="Congress number (nominations)",
    )
    parser.add_argument(
        "--state",
        default=None,
        help="2-letter USPS code (legiscan)",
    )
    parser.add_argument(
        "--all-states",
        action="store_true",
        help="All 50 states (legiscan)",
    )
    parser.add_argument(
        "--session",
        type=int,
        default=None,
        help="LegiScan session_id, or Senate session 1 or 2 (senate_votes)",
    )
    parser.add_argument(
        "--agency",
        default=None,
        help="Agency name/abbrev (usaspending or oversight)",
    )
    args = parser.parse_args()

    if args.live:
        os.environ["INGEST_LIVE"] = "1"

    if args.filing_id and args.source != "house":
        logger.error(
            "--filing-id re-ingests one House filing; pass --source house"
        )
        sys.exit(1)

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

    client = get_client() if use_hz else None

    results = {}
    with nullcontext(None) as conn:
        if args.source in ("house", "all"):
            logger.info("=== Running House ingest ===")
            house_days = args.days_back if args.days_back is not None else 90
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
                filing_id=args.filing_id,
            )

        if args.source in ("senate", "all"):
            logger.info("=== Running Senate ingest ===")
            senate_days = args.days_back if args.days_back is not None else 90
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

        if args.source in ("senate_votes", "all"):
            logger.info("=== Running Senate roll-call ingest ===")
            max_r = args.max_records
            if args.dry_run and max_r is None:
                max_r = 5
            # --days-back / --congress / --session on --source all belong to
            # the disclosure and LegiScan flags. The daily job posts the
            # full current Senate session.
            if args.source == "senate_votes":
                vote_congress = args.congress
                vote_session = args.session
                vote_days = args.days_back
            else:
                vote_congress = None
                vote_session = None
                vote_days = None
            results["senate_votes"] = senate_votes.run(
                congress=vote_congress,
                session=vote_session,
                days_back=vote_days,
                max_records=max_r,
                dry_run=args.dry_run,
                fetch_details=True,
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
            else:
                logger.warning(
                    "congress_enrich skipped — PocketBase superuser login "
                    "has been removed"
                )

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

        if args.source == "gao":
            logger.info("=== Running GAO improper-payment ingest ===")
            fy = args.fiscal_year
            if fy is None:
                logger.error("--fiscal-year is required for --source gao")
                sys.exit(1)
            max_r = args.max_records
            if args.dry_run and max_r is None:
                max_r = 3
            results["gao"] = gao.run(
                fiscal_year=fy,
                dry_run=args.dry_run,
                max_records=max_r,
                client=client,
            )

        if args.source == "oversight":
            logger.info("=== Running Oversight.gov IG ingest ===")
            max_r = args.max_records
            if args.dry_run and max_r is None:
                max_r = 3
            results["oversight"] = oversight.run(
                days_back=args.days_back if args.days_back is not None else 14,
                agency=args.agency,
                max_records=max_r,
                dry_run=args.dry_run,
                client=client,
            )

        if args.source == "prac":
            logger.info("=== Running PRAC fraud-case ingest ===")
            max_r = args.max_records
            if args.dry_run and max_r is None:
                max_r = 5
            # Writes only via `python -m ingest.prac --post`.
            results["prac"] = prac.run(
                days_back=args.days_back if args.days_back is not None else 14,
                max_records=max_r,
                dry_run=True,
                client=client,
            )

        if args.source == "fjc":
            logger.info("=== Running FJC judges ingest ===")
            max_r = args.max_records
            if args.dry_run and max_r is None:
                max_r = 5
            results["fjc"] = fjc.run(
                dry_run=args.dry_run,
                full_refresh=True,
                max_records=max_r,
                client=client,
            )

        if args.source == "jefs":
            logger.info("=== Running AO/JEFS judicial disclosure ingest ===")
            max_r = args.max_records
            if args.dry_run and max_r is None:
                max_r = 3
            results["jefs"] = jefs.run(
                judge_id=getattr(args, "judge_id", None),
                days_back=args.days_back if args.days_back is not None else 14,
                max_records=max_r,
                dry_run=args.dry_run,
                client=client,
            )

        if args.source == "nominations":
            logger.info("=== Running judicial nomination linker ===")
            max_r = args.max_records
            if args.dry_run and max_r is None:
                max_r = 10
            from .nominations import current_congress

            results["nominations"] = nominations.run(
                congress=args.congress or current_congress(),
                days_back=args.days_back if args.days_back is not None else 14,
                max_records=max_r,
                dry_run=args.dry_run,
                client=client,
            )

        if args.source == "legiscan":
            logger.info("=== Running LegiScan legislator ingest ===")
            if args.all_states:
                states = list(legiscan.STATE_CODES)
            elif args.state:
                states = [args.state.upper()]
            else:
                logger.error("--state XX or --all-states is required for --source legiscan")
                sys.exit(1)
            results["legiscan"] = legiscan.run(
                states=states,
                dry_run=args.dry_run,
                client=client,
            )

        if args.source == "legiscan_bills":
            logger.info("=== Running LegiScan bills/votes ingest ===")
            if args.all_states:
                states = list(legiscan.STATE_CODES)
            elif args.state:
                states = [args.state.upper()]
            else:
                logger.error("--state XX or --all-states is required for --source legiscan_bills")
                sys.exit(1)
            max_r = args.max_records
            if args.dry_run and max_r is None:
                max_r = 5
            results["legiscan_bills"] = legiscan_bills.run(
                states=states,
                session_id=getattr(args, "session", None),
                days_back=args.days_back if args.days_back is not None else 14,
                dry_run=args.dry_run,
                max_records=max_r,
                client=client,
            )

        if args.source == "ca_spending":
            logger.info("=== Running California Open FI$Cal ingest ===")
            fy = args.fiscal_year
            if fy is None:
                logger.error("--fiscal-year is required for --source ca_spending")
                sys.exit(1)
            max_r = args.max_records
            if max_r is None and args.max_filings is not None:
                max_r = args.max_filings
            if args.dry_run and max_r is None:
                max_r = 5
            results["ca_spending"] = ca_spending.run(
                fiscal_year=fy,
                days_back=args.days_back,
                max_records=max_r,
                dry_run=args.dry_run,
                client=client,
            )

        if args.source == "state_spending":
            logger.info("=== Running 50-state spending ingest ===")
            fy = args.fiscal_year
            if fy is None:
                logger.error("--fiscal-year is required for --source state_spending")
                sys.exit(1)
            if args.all_states:
                states = list(state_spending.STATE_CODES)
            elif args.state:
                states = [args.state.upper()]
            else:
                logger.error("--state XX or --all-states is required for --source state_spending")
                sys.exit(1)
            max_r = args.max_records
            if max_r is None and args.max_filings is not None:
                max_r = args.max_filings
            if args.dry_run and max_r is None:
                max_r = 5
            results["state_spending"] = state_spending.run(
                states=states,
                fiscal_year=fy,
                days_back=args.days_back,
                max_records=max_r,
                dry_run=args.dry_run,
                client=client,
            )

    logger.info("=== Run summary ===")
    for source, r in results.items():
        logger.info("%s: %s", source, r)
    _write_step_summary(results)
    failed = 0
    details: list[str] = []
    for r in results.values():
        if not isinstance(r, dict):
            continue
        failed += int(r.get("failed") or 0)
        for item in r.get("failures") or []:
            url = item.get("source_url") or "(no source url)"
            reason = item.get("reason") or "unknown"
            details.append(f"{url}: {reason}")
    if failed:
        # Retries that succeed never increment `failed`. A count that is
        # still above zero means the last attempt failed too.
        logger.error(
            "Ingest finished with %s failed record(s). %s",
            failed,
            "; ".join(details) or "See the log above for source_url and HTTP status.",
        )
        sys.exit(1)


def _write_step_summary(results: dict) -> None:
    """GitHub Actions job summary when GITHUB_STEP_SUMMARY is set."""
    import os

    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = ["## Disclosure ingest", ""]
    for source, result in results.items():
        lines.append(f"- **{source}**: `{result}`")
    lines.append("")
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines))
    except OSError as e:
        logger.warning("Could not write GITHUB_STEP_SUMMARY: %s", e)


if __name__ == "__main__":
    main()
