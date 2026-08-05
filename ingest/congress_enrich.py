"""Member enrichment via the official Congress.gov API.

Backfills party/state/image on existing PocketBase `members` records that
already have a bioguide_id. Does not create members from scratch — that
belongs to the congress member-sync path on the Horizons app.
"""
import logging
import time

import requests

from . import config, db

logger = logging.getLogger("rollcalltrack_ingest.congress_enrich")


def fetch_member(bioguide_id: str) -> dict | None:
    url = f"{config.CONGRESS_GOV_API_BASE}/member/{bioguide_id}"
    resp = requests.get(
        url,
        params={"api_key": config.CONGRESS_GOV_API_KEY, "format": "json"},
        timeout=config.REQUEST_TIMEOUT_SECONDS,
    )
    if resp.status_code != 200:
        logger.warning(
            "Congress.gov lookup failed for %s: %s", bioguide_id, resp.status_code
        )
        return None
    return resp.json().get("member")


def enrich_members_missing_data(conn, dry_run: bool = False) -> dict:
    updated, failed = 0, 0

    # Page through members missing party or state
    page = 1
    while True:
        data = conn.list(
            "members",
            filter='party = "" || state = "" || party = null || state = null',
            page=page,
            per_page=100,
        )
        rows = data.get("items") or []
        if not rows:
            break

        for rec in rows:
            bioguide_id = rec.get("bioguide_id") or rec.get("id")
            full_name = rec.get("name") or bioguide_id
            if not bioguide_id:
                logger.info(
                    "No bioguide_id on file for %s — needs manual resolution",
                    full_name,
                )
                failed += 1
                continue

            time.sleep(0.5)
            data_api = fetch_member(bioguide_id)
            if not data_api:
                failed += 1
                continue

            patch = {
                "party": rec.get("party") or data_api.get("partyName") or "",
                "state": rec.get("state") or data_api.get("state") or "",
                "image": rec.get("image")
                or (data_api.get("depiction") or {}).get("imageUrl")
                or "",
            }
            if dry_run:
                logger.info("[dry-run] Would update member %s with %s", full_name, patch)
                updated += 1
                continue

            conn.update("members", rec["id"], patch)
            updated += 1

        total_pages = int(data.get("totalPages") or 1)
        if page >= total_pages:
            break
        page += 1

    return {"updated": updated, "failed": failed}


def run(conn, dry_run: bool = False) -> dict:
    result = enrich_members_missing_data(conn, dry_run=dry_run)
    if not dry_run:
        db.log_ingestion_run(
            conn,
            "congress_gov",
            found=result["updated"] + result["failed"],
            parsed=result["updated"],
            failed=result["failed"],
        )
    return result
