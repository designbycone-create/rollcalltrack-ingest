"""State legislative race rows from hosted members.

No challenger names. is_term_up is true for state house / unicameral
(typically all seats each cycle) and null for state senate.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time

from .elections_catalog import load_catalog, validate_row
from .ingest_client import get_client

logger = logging.getLogger("rollcalltrack_ingest.elections_state_races")

HORIZONS_BATCH_SIZE = 25
CYCLE_DEFAULT = 2026


def race_from_member(member: dict, cycle: int, sos_by_state: dict[str, str]) -> dict | None:
    code = (member.get("state_code") or member.get("state") or "").strip().upper()
    if len(code) != 2:
        return None
    chamber = (member.get("chamber") or "").strip().lower()
    district = member.get("district")
    try:
        dist = str(int(district))
    except (TypeError, ValueError):
        dist = str(district or "").strip()
        if not dist:
            return None
    if chamber == "unicameral":
        office = "unicameral"
        key = f"leg-{dist}"
        office_name = f"{code} Legislature District {dist}"
        term_up: bool | None = True
    elif chamber == "senate":
        office = "state_senate"
        key = f"sd-{dist}"
        office_name = f"{code} State Senate District {dist}"
        term_up = None
    elif chamber == "house":
        office = "state_house"
        key = f"hd-{dist}"
        office_name = f"{code} State House District {dist}"
        term_up = True
    else:
        return None
    slug = f"{cycle}-{code.lower()}-{key}"
    portal = sos_by_state.get(code) or ""
    return {
        "slug": slug,
        "cycle": cycle,
        "jurisdiction": "state",
        "state_code": code,
        "office": office,
        "district": dist,
        "office_name": office_name,
        "is_special": False,
        "incumbent_name": (member.get("name") or "").strip() or None,
        "incumbent_bioguide_id": (member.get("bioguide_id") or "").strip() or None,
        "is_term_up": term_up,
        "sos_ballot_url": portal or None,
        "source_url": (member.get("source_url") or "").strip()
        or f"{portal}?race={slug}",
    }


def governor_race(code: str, cycle: int, portal: str) -> dict:
    slug = f"{cycle}-{code.lower()}-governor"
    return {
        "slug": slug,
        "cycle": cycle,
        "jurisdiction": "state",
        "state_code": code,
        "office": "governor",
        "district": None,
        "office_name": f"{code} Governor",
        "is_special": False,
        "is_term_up": None,
        "sos_ballot_url": portal or None,
        "source_url": f"{portal}#{slug}" if portal else f"https://www.usa.gov/state-election-office?state={code}&office=governor",
    }


def run(*, cycle: int = CYCLE_DEFAULT, dry_run: bool = False, client=None) -> dict:
    hz = client or get_client()
    catalog = [validate_row(r) for r in load_catalog()]
    sos_by_state = {
        r["state_code"]: r["sos_portal_url"]
        for r in catalog
        if r.get("jurisdiction") == "state" and r.get("state_code")
    }
    members = hz.fetch_members(jurisdiction="state") if not dry_run or client else []
    if dry_run and not members and client is not None:
        members = client.fetch_members(jurisdiction="state")
    races: dict[str, dict] = {}
    for mem in members:
        rec = race_from_member(mem, cycle, sos_by_state)
        if rec is None or not rec.get("source_url"):
            continue
        prev = races.get(rec["slug"])
        if prev is None:
            races[rec["slug"]] = rec
        elif rec.get("incumbent_name") and not prev.get("incumbent_name"):
            races[rec["slug"]] = rec
    for code, portal in sos_by_state.items():
        g = governor_race(code, cycle, portal)
        races.setdefault(g["slug"], g)
    race_list = list(races.values())
    posted = 0
    if dry_run:
        for rec in race_list[:5]:
            logger.info("dry-run state race %s", json.dumps(rec, default=str)[:400])
    else:
        if not hz.configured:
            raise RuntimeError("HORIZONS_BASE_URL and INGEST_API_KEY required")
        for i in range(0, len(race_list), HORIZONS_BATCH_SIZE):
            hz.submit_elections_races(race_list[i : i + HORIZONS_BATCH_SIZE])
            posted += len(race_list[i : i + HORIZONS_BATCH_SIZE])
            time.sleep(1.0)
    summary = {
        "members": len(members),
        "mapped": len(race_list),
        "posted": posted if not dry_run else 0,
        "dry_run": dry_run,
        "cycle": cycle,
    }
    logger.info("=== elections state races summary === %s", summary)
    return summary


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    p = argparse.ArgumentParser(description="Ingest state legislative election races")
    p.add_argument("--cycle", type=int, default=CYCLE_DEFAULT)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    run(cycle=args.cycle, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
