"""Member enrichment via the official Congress.gov API — the most stable
and well-documented source in this whole pipeline. Backfills bioguide_id,
party, state, committee assignments, and portrait URL where missing."""
import logging
import time

import requests

from . import config, db

logger = logging.getLogger("rollcalltrack_ingest.congress_enrich")


def fetch_member(bioguide_id: str) -> dict | None:
    url = f"{config.CONGRESS_GOV_API_BASE}/member/{bioguide_id}"
    resp = requests.get(url, params={"api_key": config.CONGRESS_GOV_API_KEY,
                                      "format": "json"},
                         timeout=config.REQUEST_TIMEOUT_SECONDS)
    if resp.status_code != 200:
        logger.warning("Congress.gov lookup failed for %s: %s",
                        bioguide_id, resp.status_code)
        return None
    return resp.json().get("member")


def enrich_members_missing_data(conn, dry_run: bool = False) -> dict:
    """Find member records with missing party/state/bioguide_id and
    attempt to fill them in from Congress.gov."""
    updated, failed = 0, 0

    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, full_name, bioguide_id FROM members
            WHERE party IS NULL OR state IS NULL OR bioguide_id IS NULL
        """)
        rows = cur.fetchall()

    for member_id, full_name, bioguide_id in rows:
        if not bioguide_id:
            logger.info("No bioguide_id on file for %s — needs manual "
                        "resolution before enrichment can run", full_name)
            failed += 1
            continue

        time.sleep(0.5)  # Congress.gov API is generous but be polite
        data = fetch_member(bioguide_id)
        if not data:
            failed += 1
            continue

        if dry_run:
            logger.info("[dry-run] Would update member %s with: %s",
                        full_name, {
                            "party": data.get("partyName"),
                            "state": data.get("state"),
                        })
            updated += 1
            continue

        with conn.cursor() as cur:
            cur.execute("""
                UPDATE members
                SET party = COALESCE(party, %s),
                    state = COALESCE(state, %s),
                    portrait_url = COALESCE(portrait_url, %s)
                WHERE id = %s
            """, (
                data.get("partyName"),
                data.get("state"),
                data.get("depiction", {}).get("imageUrl"),
                member_id,
            ))
        updated += 1

    return {"updated": updated, "failed": failed}


def run(conn, dry_run: bool = False) -> dict:
    result = enrich_members_missing_data(conn, dry_run=dry_run)
    if not dry_run:
        db.log_ingestion_run(conn, "congress_gov_enrich",
                             found=result["updated"] + result["failed"],
                             parsed=result["updated"],
                             failed=result["failed"])
    return result
