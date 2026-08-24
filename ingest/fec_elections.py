"""OpenFEC candidate + totals ingest for a federal cycle.

Uses an api.data.gov key (FEC_API_KEY or FOIA_GOV_API_KEY). Does not
invent receipts. State legislative challengers are out of scope.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from typing import Any

import requests

from . import config
from .ingest_client import get_client

logger = logging.getLogger("rollcalltrack_ingest.fec_elections")

FEC_BASE = "https://api.open.fec.gov/v1"
FEC_CANDIDATE_URL = "https://www.fec.gov/data/candidate/{id}/"
HORIZONS_BATCH_SIZE = 25
PAGE_DELAY_SECONDS = 0.25
PER_PAGE = 100

PARTY_MAP = {
    "DEM": "D",
    "REP": "R",
    "IND": "I",
    "NNE": "N",
    "NPA": "N",
    "NON": "N",
    "UN": "N",
}


def api_key() -> str:
    return (
        os.environ.get("FEC_API_KEY")
        or getattr(config, "FEC_API_KEY", "")
        or os.environ.get("FOIA_GOV_API_KEY")
        or getattr(config, "FOIA_GOV_API_KEY", "")
        or ""
    )


def map_party(raw: str | None) -> str:
    code = (raw or "").strip().upper()
    if code in PARTY_MAP:
        return PARTY_MAP[code]
    if code in ("D", "R", "I", "N", "O"):
        return code
    return "O"


def house_district(raw: Any) -> str:
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return "1"
    if n <= 0:
        return "1"
    return str(n)


def race_slug(cycle: int, office: str, state: str, district: str | None, special: bool) -> str:
    st = (state or "").strip().lower()
    if office == "S":
        base = f"{cycle}-{st}-us-senate"
    elif office == "H":
        base = f"{cycle}-{st}-us-house-{house_district(district)}"
    else:
        raise ValueError(f"unsupported office {office}")
    if special:
        base = f"{base}-special"
    return base


def office_name(office: str, state: str, district: str | None, special: bool) -> str:
    st = (state or "").upper()
    if office == "S":
        name = f"U.S. Senate {st}"
    else:
        name = f"U.S. House {st} District {house_district(district)}"
    if special:
        name = f"{name} special"
    return name


def fec_race_url(office: str, state: str, cycle: int, district: str | None) -> str:
    st = (state or "").upper()
    if office == "S":
        return f"https://www.fec.gov/data/elections/senate/{st}/{cycle}/"
    dist = house_district(district).zfill(2)
    return f"https://www.fec.gov/data/elections/house/{st}/{dist}/{cycle}/"


def map_totals_row(row: dict, cycle: int) -> tuple[dict, dict] | None:
    office = (row.get("office") or "").upper()
    state = (row.get("state") or "").upper()
    cid = (row.get("candidate_id") or "").strip()
    name = (row.get("name") or "").strip()
    if office not in ("H", "S") or len(state) != 2 or not cid or not name:
        return None
    if row.get("candidate_inactive") is True:
        return None
    district = row.get("district_number")
    slug = race_slug(cycle, office, state, district, special=False)
    office_enum = "us_senate" if office == "S" else "us_house"
    dist_text = None if office == "S" else house_district(district)
    incumbent = (row.get("incumbent_challenge") or "").upper() == "I"
    receipts = _num(row.get("receipts"))
    disbursements = _num(row.get("disbursements"))
    cash = _num(row.get("cash_on_hand_end_period"))
    race = {
        "slug": slug,
        "cycle": cycle,
        "jurisdiction": "federal",
        "state_code": state,
        "office": office_enum,
        "district": dist_text,
        "office_name": office_name(office, state, district, False),
        "is_special": False,
        "general_date": f"{cycle}-11-03" if cycle == 2026 else None,
        "incumbent_name": name if incumbent else None,
        "is_term_up": True,
        "source_url": fec_race_url(office, state, cycle, district),
    }
    cand = {
        "race_slug": slug,
        "cycle": cycle,
        "state_code": state,
        "fec_candidate_id": cid,
        "name": name,
        "party": map_party(row.get("party")),
        "party_raw": (row.get("party") or "").strip(),
        "is_incumbent": incumbent,
        "receipts": receipts,
        "disbursements": disbursements,
        "cash_on_hand": cash,
        "coverage_end_date": (row.get("coverage_end_date") or "")[:10] or None,
        "source_url": FEC_CANDIDATE_URL.format(id=cid),
    }
    return race, cand


def _num(raw: Any) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def fetch_totals(
    session: requests.Session,
    key: str,
    cycle: int,
    office: str,
    max_records: int | None = None,
) -> list[dict]:
    out: list[dict] = []
    page = 1
    while True:
        params = {
            "api_key": key,
            "cycle": cycle,
            "office": office,
            "per_page": PER_PAGE,
            "page": page,
            "sort": "name",
            "is_active_candidate": True,
        }
        resp = session.get(
            f"{FEC_BASE}/candidates/totals/",
            params=params,
            timeout=config.REQUEST_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        payload = resp.json()
        rows = payload.get("results") or []
        if not rows:
            break
        out.extend(rows)
        if max_records is not None and len(out) >= max_records:
            return out[:max_records]
        pag = payload.get("pagination") or {}
        pages = int(pag.get("pages") or page)
        if page >= pages:
            break
        page += 1
        time.sleep(PAGE_DELAY_SECONDS)
    return out


def map_special_date(row: dict, cycle: int) -> dict | None:
    et = (row.get("election_type_id") or "").upper()
    if et not in ("SP", "SG", "SGR", "SPR"):
        return None
    office = (row.get("office_sought") or "").upper()
    state = (row.get("election_state") or "").upper()
    if office != "H" or len(state) != 2:
        return None
    district = row.get("election_district")
    slug = race_slug(cycle, "H", state, district, special=True)
    date = (row.get("election_date") or "")[:10] or None
    return {
        "slug": slug,
        "cycle": cycle,
        "jurisdiction": "federal",
        "state_code": state,
        "office": "us_house",
        "district": house_district(district),
        "office_name": office_name("H", state, district, True),
        "is_special": True,
        "general_date": date if et in ("SG", "SGR") else None,
        "primary_date": date if et in ("SP", "SPR") else None,
        "is_term_up": True,
        "source_url": fec_race_url("H", state, cycle, district) + "#special",
    }


def fetch_election_dates(
    session: requests.Session,
    key: str,
    year: int,
) -> list[dict]:
    out: list[dict] = []
    page = 1
    while True:
        params = {
            "api_key": key,
            "election_year": year,
            "per_page": PER_PAGE,
            "page": page,
            "sort": "election_date",
        }
        resp = session.get(
            f"{FEC_BASE}/election-dates/",
            params=params,
            timeout=config.REQUEST_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        payload = resp.json()
        rows = payload.get("results") or []
        if not rows:
            break
        out.extend(rows)
        pag = payload.get("pagination") or {}
        pages = int(pag.get("pages") or page)
        if page >= pages:
            break
        page += 1
        time.sleep(PAGE_DELAY_SECONDS)
    return out


def run(*, cycle: int = 2026, dry_run: bool = False, max_records: int | None = None, client=None) -> dict:
    key = api_key()
    if not key:
        raise RuntimeError("FEC_API_KEY or FOIA_GOV_API_KEY is required")
    session = requests.Session()
    session.headers.update({"User-Agent": config.USER_AGENT})
    rows: list[dict] = []
    for office in ("S", "H"):
        cap = None if max_records is None else max(0, max_records - len(rows))
        if cap == 0:
            break
        rows.extend(fetch_totals(session, key, cycle, office, max_records=cap))
    races: dict[str, dict] = {}
    cands: list[dict] = []
    skipped = 0
    for row in rows:
        mapped = map_totals_row(row, cycle)
        if mapped is None:
            skipped += 1
            continue
        race, cand = mapped
        if race["slug"] not in races:
            races[race["slug"]] = race
        elif cand.get("is_incumbent") and not races[race["slug"]].get("incumbent_name"):
            races[race["slug"]]["incumbent_name"] = cand["name"]
        cands.append(cand)
    for drow in fetch_election_dates(session, key, cycle):
        spec = map_special_date(drow, cycle)
        if spec and spec["slug"] not in races:
            races[spec["slug"]] = spec
    race_list = list(races.values())
    posted_races = 0
    posted_cands = 0
    if dry_run:
        for rec in race_list[:5]:
            logger.info("dry-run race %s", json.dumps(rec, default=str)[:400])
        for rec in cands[:5]:
            logger.info("dry-run candidate %s", json.dumps(rec, default=str)[:400])
    else:
        hz = client or get_client()
        if not hz.configured:
            raise RuntimeError("HORIZONS_BASE_URL and INGEST_API_KEY required")
        for i in range(0, len(race_list), HORIZONS_BATCH_SIZE):
            hz.submit_elections_races(race_list[i : i + HORIZONS_BATCH_SIZE])
            posted_races += len(race_list[i : i + HORIZONS_BATCH_SIZE])
            time.sleep(1.0)
        for i in range(0, len(cands), HORIZONS_BATCH_SIZE):
            hz.submit_elections_candidates(cands[i : i + HORIZONS_BATCH_SIZE])
            posted_cands += len(cands[i : i + HORIZONS_BATCH_SIZE])
            time.sleep(1.0)
    summary = {
        "found": len(rows),
        "races": len(race_list),
        "candidates": len(cands),
        "skipped": skipped,
        "posted_races": posted_races if not dry_run else 0,
        "posted_candidates": posted_cands if not dry_run else 0,
        "dry_run": dry_run,
        "cycle": cycle,
    }
    logger.info("=== fec elections summary === %s", summary)
    return summary


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    p = argparse.ArgumentParser(description="Ingest OpenFEC candidates and totals")
    p.add_argument("--cycle", type=int, default=2026)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--max-records", type=int, default=None)
    args = p.parse_args()
    run(cycle=args.cycle, dry_run=args.dry_run, max_records=args.max_records)
    return 0


if __name__ == "__main__":
    sys.exit(main())
