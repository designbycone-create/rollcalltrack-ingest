"""LegiScan state legislator roster ingest (legiscan-legislators-sync).

Maps onto the existing Horizons ``members`` collection with
``jurisdiction=state`` + ``state_code``. No parallel state_members table.

Live hooks (api.legiscan.com):
  getSessionList(state) → current session
  getSessionPeople(session_id) → full roster (one query, not getPerson × N)

Federal Congress is not pulled here. 50 states only (no DC).
Nebraska is always chamber=unicameral.

Party matches the live federal members feed: D / R / I.
Anything else (nonpartisan, third party) goes to manual_review.

bioguide_id for state rows is ``LS{people_id}`` so it cannot collide
with a congressional bioguide (those are letter + 6 digits).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import config
from .ingest_client import get_client

logger = logging.getLogger("rollcalltrack_ingest.legiscan")

LEGISCAN_BASE = "https://api.legiscan.com/"
PAGE_DELAY_SECONDS = 0.4
HORIZONS_BATCH_SIZE = 50

# 50 states. DC and US (Congress) are out of scope.
STATE_CODES = (
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
)

CHAMBER_ENUM = ("house", "senate", "unicameral")
# Same letter codes as federal D/R/I, plus N = Nonpartisan (LegiScan / FEC).
# N is not Independent: NE's unicameral is officially nonpartisan.
PARTY_ENUM = ("D", "R", "I", "N")
TEST_STATES = ("DE", "NE", "WV", "TX", "CA")

DISTRICT_NUM_RE = re.compile(r"(\d+)")


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": config.USER_AGENT})
    retry = Retry(
        total=5,
        connect=3,
        read=3,
        status=5,
        backoff_factor=2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def api_key() -> str:
    return (os.environ.get("LEGISCAN_API_KEY") or "").strip()


def source_url_for(state: str, people_id: int | str) -> str:
    return f"https://legiscan.com/{state.upper()}/people/{people_id}"


def member_id_for(people_id: int | str) -> str:
    return f"LS{people_id}"


def normalize_party(raw: str | None) -> str | None:
    """Letter codes on the existing party field.

    Federal members are D / R / I. LegiScan ``N`` is Nonpartisan (also
    FEC's code), which is how Nebraska's unicameral is officially labeled.
    That is not Independent and is not missing data.
    """
    s = (raw or "").strip().upper()
    if s in ("D", "DEMOCRAT", "DEMOCRATIC", "DEM"):
        return "D"
    if s in ("R", "REPUBLICAN", "GOP"):
        return "R"
    if s in ("I", "IND", "INDEPENDENT"):
        return "I"
    if s in ("N", "NP", "NPP", "NONPARTISAN", "NON-PARTISAN", "NON PARTISAN"):
        return "N"
    return None


def normalize_chamber(state: str, role: str | None) -> str | None:
    if state.upper() == "NE":
        return "unicameral"
    r = (role or "").strip().lower()
    if r in ("sen", "senate", "senator"):
        return "senate"
    if r in ("rep", "house", "representative", "assembly", "delegate"):
        return "house"
    return None


def parse_district(raw: str | None) -> int | None:
    """Existing members.district is a number. HD-111 / SD-3 / 12 → int.

    Non-numeric labels are not guessed.
    """
    s = (raw or "").strip()
    if not s:
        return None
    m = DISTRICT_NUM_RE.search(s)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def pick_current_session(sessions: list[dict]) -> dict | None:
    if not sessions:
        return None
    active = [
        s
        for s in sessions
        if not int(s.get("sine_die") or 0) and not int(s.get("prior") or 0)
    ]
    pool = active or list(sessions)
    regular = [s for s in pool if not int(s.get("special") or 0)]
    pool = regular or pool
    return max(
        pool,
        key=lambda s: (int(s.get("year_end") or 0), int(s.get("session_id") or 0)),
    )


def map_person(person: dict, state: str) -> dict:
    people_id = person.get("people_id")
    name = (person.get("name") or "").strip()
    if int(person.get("committee_sponsor") or 0) == 1:
        return {
            "_skip": True,
            "reason": "committee_sponsor",
            "name": name,
            "people_id": people_id,
        }
    if not people_id or not name:
        return {
            "_review": True,
            "reason": "unexpected_structure",
            "notes": f"{state} person missing people_id or name",
            "raw_name": name,
        }
    party = normalize_party(person.get("party"))
    chamber = normalize_chamber(state, person.get("role"))
    district = parse_district(person.get("district"))
    district_raw = (person.get("district") or "").strip()
    notes = []
    if party is None:
        notes.append(f"unmapped party={person.get('party')!r}")
    if chamber is None:
        notes.append(f"unmapped role={person.get('role')!r}")
    if district is None and district_raw:
        notes.append(f"non-numeric district={district_raw!r}")
    if notes:
        return {
            "_review": True,
            "reason": "unexpected_structure",
            "notes": f"{state} {name}: " + "; ".join(notes),
            "people_id": people_id,
            "name": name,
            "state_code": state.upper(),
            "source_url": source_url_for(state, people_id),
        }
    role = person.get("role") or ""
    role_label = {
        "house": "Representative",
        "senate": "Senator",
        "unicameral": "Senator",
    }.get(chamber or "", role)
    if district is not None:
        role_out = f"{role_label}, District {district}"
    else:
        role_out = role_label
    return {
        "bioguide_id": member_id_for(people_id),
        "name": name,
        "party": party,
        "state": state.upper(),
        "chamber": chamber,
        "district": district,
        "role": role_out,
        "jurisdiction": "state",
        "state_code": state.upper(),
        "source_url": source_url_for(state, people_id),
    }


def legiscan_get(
    session: requests.Session, op: str, params: dict | None = None
) -> dict:
    key = api_key()
    if not key:
        raise RuntimeError("LEGISCAN_API_KEY is required")
    q = {"key": key, "op": op}
    if params:
        q.update(params)
    resp = session.get(LEGISCAN_BASE, params=q, timeout=config.REQUEST_TIMEOUT_SECONDS)
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") == "ERROR":
        msg = ((data.get("alert") or {}).get("message")) or str(data)
        raise RuntimeError(f"LegiScan {op} error: {msg}")
    return data


def fetch_roster(session: requests.Session, state: str) -> tuple[dict, list[dict]]:
    listing = legiscan_get(session, "getSessionList", {"state": state.upper()})
    sessions = listing.get("sessions") or []
    current = pick_current_session(sessions)
    if not current:
        raise RuntimeError(f"No LegiScan sessions for {state}")
    sid = current["session_id"]
    people_payload = legiscan_get(session, "getSessionPeople", {"id": sid})
    block = people_payload.get("sessionpeople") or people_payload
    people = block.get("people") or []
    return current, people


def run(
    states: list[str],
    dry_run: bool = False,
    client=None,
    rosters: dict[str, list[dict]] | None = None,
) -> dict:
    mapped: list[dict] = []
    review: list[dict] = []
    per_state: dict[str, dict] = {}
    session = None if rosters is not None else _session()

    for state in states:
        code = state.upper()
        if code not in STATE_CODES:
            raise ValueError(f"Not a 50-state USPS code: {state}")
        if rosters is not None:
            people = rosters.get(code) or []
            session_meta = {"session_id": None, "session_title": "fixture"}
        else:
            session_meta, people = fetch_roster(session, code)
            time.sleep(PAGE_DELAY_SECONDS)
        ok = 0
        flagged = 0
        skipped = 0
        parties: dict[str, int] = {}
        for person in people:
            rec = map_person(person, code)
            if rec.get("_skip"):
                skipped += 1
                continue
            if rec.get("_review"):
                review.append(rec)
                flagged += 1
            else:
                mapped.append(rec)
                ok += 1
                parties[rec["party"]] = parties.get(rec["party"], 0) + 1
        per_state[code] = {
            "found": len(people),
            "mapped": ok,
            "review": flagged,
            "skipped": skipped,
            "session": session_meta.get("session_title") or session_meta.get("session_name"),
            "session_id": session_meta.get("session_id"),
            "parties": parties,
        }
        logger.info(
            "legiscan %s session=%s found=%s mapped=%s review=%s parties=%s",
            code,
            per_state[code]["session"],
            len(people),
            ok,
            flagged,
            parties,
        )

    posted = 0
    if dry_run:
        for rec in mapped[:5]:
            logger.info("dry-run member %s", json.dumps(rec, default=str)[:400])
        for rec in review[:5]:
            logger.info("dry-run review %s", json.dumps(rec, default=str)[:400])
    else:
        hz = client or get_client()
        if not hz.configured:
            raise RuntimeError("HORIZONS_BASE_URL and INGEST_API_KEY required")
        for i in range(0, len(mapped), HORIZONS_BATCH_SIZE):
            hz.submit_members(mapped[i : i + HORIZONS_BATCH_SIZE])
            posted += len(mapped[i : i + HORIZONS_BATCH_SIZE])
        for rec in review:
            hz.flag_manual_review(
                filing_id=str(rec.get("people_id") or rec.get("name") or "unknown"),
                reason=rec.get("reason") or "unexpected_structure",
                notes=rec.get("notes") or "",
            )
        hz.submit_ingestion_log(
            source="legiscan",
            found=sum(v["found"] for v in per_state.values()),
            parsed=len(mapped),
            failed=len(review),
            flagged=len(review),
            note="states=" + ",".join(states),
        )
        from .legiscan_bills import coverage_row, utc_now

        cov = []
        for code, stats in per_state.items():
            if stats["mapped"] == 0 and stats["found"] > 0:
                note = "partial, roster present but no rows mapped"
            else:
                note = "full session roster available"
            cov.append(
                coverage_row(
                    code,
                    legislators_ts=utc_now(),
                    note=note,
                    error=None,
                )
            )
        try:
            hz.submit_state_coverage(cov)
        except Exception as e:
            logger.warning("state_coverage post failed: %s", e)

    return {
        "found": sum(v["found"] for v in per_state.values()),
        "mapped": len(mapped),
        "review": len(review),
        "posted": posted,
        "dry_run": dry_run,
        "states": per_state,
    }


def resolve_states(state: str | None, all_states: bool) -> list[str]:
    if all_states and state:
        raise ValueError("Pass --state or --all-states, not both")
    if all_states:
        return list(STATE_CODES)
    if state:
        return [state.upper()]
    raise ValueError("Pass --state XX or --all-states")


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="LegiScan state legislator roster")
    p.add_argument("--state", default=None, help="Single 2-letter USPS code")
    p.add_argument("--all-states", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = build_arg_parser().parse_args(argv)
    try:
        config.require_config(dry_run=args.dry_run, source="legiscan")
        states = resolve_states(args.state, args.all_states)
    except (RuntimeError, ValueError) as e:
        logger.error(str(e))
        return 1
    try:
        result = run(states=states, dry_run=args.dry_run)
    except (RuntimeError, ValueError) as e:
        logger.error(str(e))
        return 1
    logger.info("=== legiscan summary === %s", result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
