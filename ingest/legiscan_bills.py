"""LegiScan state bills + roll-call ingest (legiscan-bills-votes-sync).

Maps onto the existing Horizons bills / votes collections with
``jurisdiction=state`` + ``state_code``. Member votes join on
``LS{people_id}`` from legiscan-legislators-sync — no name matching.

Also writes ``state_coverage`` so we can see which states are live,
partial, or failed.

Live hooks:
  getSessionList / getMasterList / getBill / getRollCall

Federal Congress is not pulled here.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

from . import config
from .ingest_client import get_client
from .legiscan import (
    PAGE_DELAY_SECONDS,
    STATE_CODES,
    _session,
    legiscan_get,
    member_id_for,
    pick_current_session,
    resolve_states,
)

logger = logging.getLogger("rollcalltrack_ingest.legiscan_bills")

HORIZONS_BATCH_SIZE = 25

BILL_STATUS = {
    1: "Introduced",
    2: "Engrossed",
    3: "Enrolled",
    4: "Passed",
    5: "Vetoed",
    6: "Failed",
}

VOTE_TEXT = {
    "yea": "yea",
    "yes": "yea",
    "aye": "yea",
    "nay": "nay",
    "no": "nay",
    "nv": "not_voting",
    "not voting": "not_voting",
    "absent": "not_voting",
    "present": "present",
    "excused": "not_voting",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_date(value: Any) -> str | None:
    s = ("" if value is None else str(value)).strip()
    if not s or s.startswith("0000"):
        return None
    if "T" in s:
        s = s.split("T", 1)[0]
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return s
    except ValueError:
        return None


def parse_bill_type(number: str) -> str:
    s = (number or "").strip().upper()
    i = 0
    while i < len(s) and s[i].isalpha():
        i += 1
    return s[:i] or "B"


def bill_source_url(bill: dict, state: str, number: str, year: int) -> str:
    url = (bill.get("url") or "").strip()
    if url.startswith("http"):
        return url
    return f"https://legiscan.com/{state.upper()}/bill/{number}/{year}"


def vote_source_url(stub: dict, state: str, number: str, roll_call_id: int) -> str:
    url = (stub.get("url") or "").strip()
    if url.startswith("http"):
        return url
    return f"https://legiscan.com/{state.upper()}/rollcall/{number}/id/{roll_call_id}"


def map_chamber(raw: str | None, state: str) -> str:
    if state.upper() == "NE":
        return "unicameral"
    r = (raw or "").strip().upper()
    if r in ("S", "SEN", "SENATE"):
        return "senate"
    return "house"


def map_member_vote(raw: str | None) -> str | None:
    s = (raw or "").strip().lower()
    return VOTE_TEXT.get(s)


def map_bill(bill: dict, state: str) -> dict:
    bill_id = bill.get("bill_id")
    number = (bill.get("bill_number") or bill.get("number") or "").strip()
    session = bill.get("session") or {}
    year = int(session.get("year_start") or 0) or date.today().year
    url = bill_source_url(bill, state, number, year)
    if not bill_id or not number or not url:
        return {
            "_review": True,
            "reason": "unexpected_structure",
            "notes": f"{state} bill missing id/number/url",
        }
    status = int(bill.get("status") or 0)
    last = ""
    hist = bill.get("history") or []
    if hist:
        last = (hist[-1].get("action") or "").strip()
    last = last or (bill.get("last_action") or "").strip()
    last_date = iso_date(bill.get("status_date")) or iso_date(
        (hist[-1].get("date") if hist else None) or bill.get("last_action_date")
    )
    became = bool(
        re.search(r"signed by governor|enact w/?o sign|became law", last, re.I)
    )
    return {
        "id": f"LS{bill_id}",
        "congress": year,
        "bill_type": (bill.get("bill_type") or parse_bill_type(number)).strip() or parse_bill_type(number),
        "bill_number": number,
        "title": (bill.get("title") or "").strip(),
        "short_title": "",
        "legislation_status": BILL_STATUS.get(status) or "",
        "became_law": became,
        "latest_action_text": last,
        "latest_action_date": last_date,
        "description": (bill.get("description") or "").strip(),
        "jurisdiction": "state",
        "state_code": state.upper(),
        "source_url": url,
        "session_id": bill.get("session_id") or session.get("session_id"),
    }


def map_vote(roll: dict, stub: dict, bill: dict, state: str) -> dict:
    rid = roll.get("roll_call_id") or stub.get("roll_call_id")
    number = (bill.get("bill_number") or bill.get("number") or "").strip()
    session = bill.get("session") or {}
    year = int(session.get("year_start") or 0) or date.today().year
    url = vote_source_url(stub or roll, state, number, rid)
    if not rid or not url:
        return {
            "_review": True,
            "reason": "unexpected_structure",
            "notes": f"{state} {number} roll call missing id/url",
        }
    rc_id = f"LSRC{rid}"
    passed = int((roll or stub).get("passed") or 0) == 1
    chamber = map_chamber((roll or stub).get("chamber"), state)
    members = []
    skipped_no_id = 0
    for row in roll.get("votes") or []:
        pid = row.get("people_id")
        if not pid:
            skipped_no_id += 1
            continue
        choice = map_member_vote(row.get("vote_text"))
        if choice is None:
            continue
        members.append(
            {
                "rc_id": rc_id,
                "bioguide_id": member_id_for(pid),
                "vote": choice,
                "name": "",
                "jurisdiction": "state",
                "state_code": state.upper(),
            }
        )
    nv = int((roll or stub).get("nv") or 0) + int((roll or stub).get("absent") or 0)
    rec = {
        "id": f"{state.lower()}-{year}-{rid}",
        "rc_id": rc_id,
        "congress": year,
        "chamber": chamber,
        "session": str(year),
        "roll_number": str(rid),
        "bill_label": number,
        "title": (roll.get("desc") or stub.get("desc") or "").strip(),
        "description": (bill.get("title") or "").strip(),
        "result": "Passed" if passed else "Failed",
        "vote_type": (roll.get("desc") or stub.get("desc") or "").strip(),
        "date": iso_date(roll.get("date") or stub.get("date")),
        "tally_yea": int((roll or stub).get("yea") or 0),
        "tally_nay": int((roll or stub).get("nay") or 0),
        "tally_present": 0,
        "tally_not_voting": nv,
        "jurisdiction": "state",
        "state_code": state.upper(),
        "source_url": url,
        "member_votes": members,
    }
    if skipped_no_id:
        rec["_skipped_no_people_id"] = skipped_no_id
    return rec


def masterlist_bills(payload: dict) -> list[dict]:
    ml = payload.get("masterlist") or payload
    out = []
    for v in ml.values() if isinstance(ml, dict) else ml:
        if isinstance(v, dict) and v.get("bill_id"):
            out.append(v)
    return out


def in_window(item: dict, since: date | None) -> bool:
    if since is None:
        return True
    d = iso_date(item.get("last_action_date") or item.get("status_date"))
    if not d:
        return False
    return d >= since.isoformat()


def coverage_row(
    state: str,
    *,
    legislators_ts: str | None = None,
    bills_ts: str | None = None,
    note: str = "",
    error: str | None = None,
) -> dict:
    return {
        "state_code": state.upper(),
        "legislators_last_synced": legislators_ts,
        "bills_votes_last_synced": bills_ts,
        "completeness_note": note,
        "last_error": error,
    }


def completeness_for_bills(
    master_n: int, window_n: int, bills_n: int, votes_n: int, error: str | None
) -> str:
    if error:
        return "sync error — see last_error"
    if master_n == 0:
        return "no bills in session (LegiScan empty or session not loaded)"
    if window_n == 0:
        return "full session list available; nothing in the incremental window"
    if votes_n == 0:
        return "partial, bills in window but no roll calls attached"
    return "full session data available; incremental window has bills and roll calls"


def run(
    states: list[str],
    session_id: int | None = None,
    days_back: int = 14,
    dry_run: bool = False,
    max_records: int | None = None,
    client=None,
    fixtures: dict | None = None,
) -> dict:
    since = date.today() - timedelta(days=days_back) if days_back else None
    http = None if fixtures is not None else _session()
    bills_out: list[dict] = []
    votes_out: list[dict] = []
    review: list[dict] = []
    coverage: list[dict] = []
    per_state: dict[str, dict] = {}

    for state in states:
        code = state.upper()
        if code not in STATE_CODES:
            raise ValueError(f"Not a 50-state USPS code: {state}")
        err = None
        note = ""
        try:
            if fixtures is not None:
                fx = fixtures.get(code) or {}
                session_meta = fx.get("session") or {"session_id": session_id, "year_start": 2025}
                master = fx.get("masterlist") or []
                details = fx.get("bills") or {}
                rolls = fx.get("roll_calls") or {}
            else:
                listing = legiscan_get(http, "getSessionList", {"state": code})
                sessions = listing.get("sessions") or []
                if session_id:
                    session_meta = next(
                        (s for s in sessions if int(s.get("session_id") or 0) == int(session_id)),
                        {"session_id": session_id},
                    )
                    sid = int(session_id)
                else:
                    session_meta = pick_current_session(sessions)
                    if not session_meta:
                        raise RuntimeError(f"No LegiScan sessions for {code}")
                    sid = int(session_meta["session_id"])
                time.sleep(PAGE_DELAY_SECONDS)
                master_payload = legiscan_get(http, "getMasterList", {"id": sid})
                master = masterlist_bills(master_payload)
                details, rolls = {}, {}
            window = [b for b in master if in_window(b, since)]
            if max_records is not None:
                window = window[:max_records]
            mapped_b = 0
            mapped_v = 0
            for stub in window:
                bid = stub["bill_id"]
                if fixtures is not None:
                    detail = details.get(str(bid)) or details.get(bid) or stub
                else:
                    time.sleep(PAGE_DELAY_SECONDS)
                    detail = (legiscan_get(http, "getBill", {"id": bid}).get("bill") or {})
                    # carry masterlist last_action onto detail
                    detail.setdefault("last_action", stub.get("last_action"))
                    detail.setdefault("last_action_date", stub.get("last_action_date"))
                    detail.setdefault("number", stub.get("number"))
                rec = map_bill(detail, code)
                if rec.get("_review"):
                    review.append(rec)
                    continue
                bills_out.append(rec)
                mapped_b += 1
                for vstub in detail.get("votes") or []:
                    rid = vstub.get("roll_call_id")
                    if not rid:
                        review.append(
                            {
                                "_review": True,
                                "reason": "unexpected_structure",
                                "notes": f"{code} {rec['bill_number']} vote stub missing roll_call_id",
                            }
                        )
                        continue
                    if fixtures is not None:
                        roll = rolls.get(str(rid)) or rolls.get(rid) or vstub
                    else:
                        time.sleep(PAGE_DELAY_SECONDS)
                        roll = (
                            legiscan_get(http, "getRollCall", {"id": rid}).get("roll_call")
                            or {}
                        )
                    vrec = map_vote(roll, vstub, detail, code)
                    if vrec.get("_review"):
                        review.append(vrec)
                        continue
                    votes_out.append(vrec)
                    mapped_v += 1
            note = completeness_for_bills(len(master), len(window), mapped_b, mapped_v, None)
            per_state[code] = {
                "master": len(master),
                "window": len(window),
                "bills": mapped_b,
                "votes": mapped_v,
                "session_id": session_meta.get("session_id") if fixtures is None else session_meta.get("session_id"),
                "note": note,
            }
        except Exception as e:
            err = str(e)
            note = completeness_for_bills(0, 0, 0, 0, err)
            per_state[code] = {
                "master": 0,
                "window": 0,
                "bills": 0,
                "votes": 0,
                "session_id": session_id,
                "note": note,
                "error": err,
            }
            logger.error("legiscan bills %s failed: %s", code, e)
        coverage.append(
            coverage_row(
                code,
                bills_ts=None if err else utc_now(),
                note=note,
                error=err,
            )
        )
        logger.info("legiscan bills %s %s", code, per_state[code])

    posted_b = posted_v = posted_c = 0
    if dry_run:
        for rec in bills_out[:5]:
            logger.info("dry-run bill %s", json.dumps(rec, default=str)[:400])
        for rec in votes_out[:3]:
            slim = {k: rec[k] for k in rec if k != "member_votes"}
            slim["member_votes"] = len(rec.get("member_votes") or [])
            logger.info("dry-run vote %s", json.dumps(slim, default=str)[:400])
        for rec in coverage:
            logger.info("dry-run coverage %s", rec)
    else:
        hz = client or get_client()
        if not hz.configured:
            raise RuntimeError("HORIZONS_BASE_URL and INGEST_API_KEY required")
        for i in range(0, len(bills_out), HORIZONS_BATCH_SIZE):
            hz.submit_bills(bills_out[i : i + HORIZONS_BATCH_SIZE])
            posted_b += len(bills_out[i : i + HORIZONS_BATCH_SIZE])
        for i in range(0, len(votes_out), HORIZONS_BATCH_SIZE):
            hz.submit_votes(votes_out[i : i + HORIZONS_BATCH_SIZE])
            posted_v += len(votes_out[i : i + HORIZONS_BATCH_SIZE])
        hz.submit_state_coverage(coverage)
        posted_c = len(coverage)
        for rec in review:
            hz.flag_manual_review(
                filing_id=rec.get("bill_number") or rec.get("rc_id") or "unknown",
                reason=rec.get("reason") or "unexpected_structure",
                notes=rec.get("notes") or "",
            )
        hz.submit_ingestion_log(
            source="legiscan",
            found=sum(v["master"] for v in per_state.values()),
            parsed=len(bills_out) + len(votes_out),
            failed=len(review),
            flagged=len(review),
            note="bills-votes states=" + ",".join(states),
        )

    return {
        "bills": len(bills_out),
        "votes": len(votes_out),
        "review": len(review),
        "posted_bills": posted_b,
        "posted_votes": posted_v,
        "posted_coverage": posted_c,
        "dry_run": dry_run,
        "days_back": days_back,
        "states": per_state,
        "coverage": coverage,
    }


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="LegiScan state bills + roll calls")
    p.add_argument("--state", default=None)
    p.add_argument("--all-states", action="store_true")
    p.add_argument("--session", type=int, default=None, help="LegiScan session_id")
    p.add_argument("--days-back", type=int, default=14)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--max-records", type=int, default=None, help="Cap bills after date filter")
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
        result = run(
            states=states,
            session_id=args.session,
            days_back=args.days_back,
            dry_run=args.dry_run,
            max_records=args.max_records,
        )
    except (RuntimeError, ValueError) as e:
        logger.error(str(e))
        return 1
    logger.info("=== legiscan bills/votes summary === %s", result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
