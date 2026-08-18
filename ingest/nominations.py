"""Judicial nomination → FJC judge + Senate roll-call linker.

Pulls presidential nominations from the Congress.gov API (same key as
``congress_enrich``) and keeps only judicial-branch positions. It does
not scrape a new website.

Live-verified (2026-08):
  GET https://api.congress.gov/v3/nomination/{congress}
  GET https://api.congress.gov/v3/nomination/{congress}/{number}
  GET https://api.congress.gov/v3/nomination/{congress}/{number}/actions
  GET https://rollcalltrack.com/hcgi/api/congress/votes
      vote id scheme: s-{congress}-{session_year}-{roll}
      e.g. s-119-2026-209 = PN999-5 Pozos confirmation
      Cloture votes are separate and are not the confirmation link.

``senate_vote_id`` is set only from an official Record Vote Number or a
Horizons vote whose title is ``On the Nomination {PN}``. Voice vote /
unanimous consent stay null with ``senate_vote_note`` set so review can
tell those apart from a roll call we could not find.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from datetime import date, datetime, timedelta
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import config
from .ingest_client import get_client
from .jefs import load_judges, match_judge

logger = logging.getLogger("rollcalltrack_ingest.nominations")

PAGE_DELAY_SECONDS = 0.5
LIST_LIMIT = 250
HORIZONS_BATCH_SIZE = 25
VOTES_FETCH_LIMIT = 250

RESULT_ENUM = ("confirmed", "rejected", "withdrawn", "returned")

JUDICIAL_RE = re.compile(
    r"(?:to be|be an?)\s+"
    r"(?:an?\s+)?"
    r"(?:associate\s+justice|chief\s+justice|"
    r"united\s+states\s+(?:district|circuit|magistrate|bankruptcy)\s+judge|"
    r"judge\s+of\s+the\s+united\s+states\s+court\s+of|"
    r"united\s+states\s+circuit\s+judge|"
    r"united\s+states\s+district\s+judge)",
    re.I,
)
# Fallback for "United States District Judge for the …" without "to be"
JUDICIAL_TITLE_RE = re.compile(
    r"united\s+states\s+(?:district|circuit|magistrate|bankruptcy)\s+judge|"
    r"associate\s+justice\s+of\s+the\s+supreme\s+court|"
    r"chief\s+justice\s+of\s+the\s+united\s+states|"
    r"judge\s+of\s+the\s+united\s+states\s+court\s+of\s+"
    r"(?:international\s+trade|federal\s+claims|appeals)",
    re.I,
)
JUDICIARY_ORG_RE = re.compile(r"^the\s+judiciary$", re.I)
JUDGE_ADVOCATE_RE = re.compile(r"judge\s+advocate", re.I)

NAME_RE = re.compile(
    r"^\s*(?P<name>.+?),\s+of\s+[A-Za-z][A-Za-z .]+\s*,\s+to be\s+(?P<rest>.+)$",
    re.I | re.S,
)
SUPREME_RE = re.compile(
    r"(associate justice of the supreme court|chief justice of the united states)",
    re.I,
)
RECORD_VOTE_RE = re.compile(r"record vote number:\s*(\d+)", re.I)
YEA_NAY_RE = re.compile(r"yea-?nay vote", re.I)


def current_congress(today: date | None = None) -> int:
    d = today or date.today()
    year = d.year
    if d.month == 1 and d.day < 3:
        year -= 1
    return (year - 1789) // 2 + 1


def congress_ordinal(n: int) -> str:
    if 10 <= (n % 100) <= 20:
        suf = "th"
    else:
        suf = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suf}"


def source_url_for(congress: int, number: int | str) -> str:
    return (
        f"https://www.congress.gov/nomination/"
        f"{congress_ordinal(int(congress))}-congress/{number}"
    )


def iso_date(value: Any) -> str | None:
    s = ("" if value is None else str(value)).strip()
    if not s:
        return None
    if "T" in s:
        s = s.split("T", 1)[0]
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def is_judicial(text: str | None) -> bool:
    blob = text or ""
    if JUDGE_ADVOCATE_RE.search(blob):
        return False
    if JUDICIARY_ORG_RE.search(blob.strip()):
        return True
    return bool(JUDICIAL_RE.search(blob) or JUDICIAL_TITLE_RE.search(blob))


def parse_nominee(description: str) -> tuple[str | None, str | None]:
    """Return (full_name, court phrase) from a Congress.gov description."""
    s = re.sub(r"\s+", " ", description or "").strip()
    m = NAME_RE.match(s)
    name = m.group("name").strip() if m else None
    rest = m.group("rest") if m else s
    if SUPREME_RE.search(rest):
        return name, "Supreme Court of the United States"
    for_m = re.search(r"for the\s+(.+?)(?:[.,;]|vice\b|$)", rest, re.I)
    if for_m:
        tail = for_m.group(1).strip()
        if re.search(r"circuit", rest, re.I):
            return name, f"U.S. Court of Appeals for the {tail}"
        return name, f"U.S. District Court for the {tail}"
    return name, None


def map_vote_result(text: str | None) -> str | None:
    t = (text or "").lower()
    if "confirmed by the senate" in t or t.startswith("confirmed by"):
        return "confirmed"
    if "rejected by the senate" in t:
        return "rejected"
    if re.search(r"\bwithdrawn\b", t):
        return "withdrawn"
    if re.search(r"returned to the (president|white house)", t):
        return "returned"
    return None


def classify_vote_link(text: str | None) -> tuple[int | None, str]:
    """Return (roll_number or None, senate_vote_note)."""
    t = text or ""
    low = t.lower()
    m = RECORD_VOTE_RE.search(t)
    if m:
        return int(m.group(1)), "roll_call"
    if "voice vote" in low:
        return None, "voice_vote"
    if "unanimous consent" in low:
        return None, "unanimous_consent"
    if YEA_NAY_RE.search(t):
        return None, "roll_call_unmatched"
    return None, "no_recorded_vote"


def senate_vote_id_for(congress: int, vote_date: str | None, roll: int) -> str:
    year = (vote_date or "")[:4]
    if not year.isdigit():
        year = str(date.today().year)
    return f"s-{int(congress)}-{year}-{int(roll)}"


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


def _api_get(session: requests.Session, path: str, extra: dict | None = None) -> dict:
    if not config.CONGRESS_GOV_API_KEY:
        raise RuntimeError("CONGRESS_GOV_API_KEY is required for nominations")
    params = {"api_key": config.CONGRESS_GOV_API_KEY, "format": "json"}
    if extra:
        params.update(extra)
    url = f"{config.CONGRESS_GOV_API_BASE}{path}"
    resp = session.get(url, params=params, timeout=config.REQUEST_TIMEOUT_SECONDS)
    if resp.status_code != 200:
        raise RuntimeError(f"Congress.gov {path} HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.json()


def list_nominations(
    session: requests.Session,
    congress: int,
    since: date | None = None,
    max_records: int | None = None,
) -> list[dict]:
    extra: dict[str, Any] = {"limit": LIST_LIMIT, "sort": "updateDate+desc"}
    # A long window means "this congress", not "updated in the last N days".
    # Congress.gov fromDateTime filters updateDate and drops older confirmations.
    if since and (date.today() - since).days < 360:
        extra["fromDateTime"] = f"{since.isoformat()}T00:00:00Z"
    out: list[dict] = []
    offset = 0
    while True:
        extra["offset"] = offset
        data = _api_get(session, f"/nomination/{congress}", extra)
        items = data.get("nominations") or []
        if not items:
            break
        for item in items:
            out.append(item)
            if max_records is not None and len(out) >= max_records:
                return out
        if len(items) < LIST_LIMIT:
            break
        offset += LIST_LIMIT
        time.sleep(PAGE_DELAY_SECONDS)
    return out


def fetch_actions(session: requests.Session, congress: int, number: int | str) -> list[dict]:
    data = _api_get(session, f"/nomination/{congress}/{number}/actions")
    return data.get("actions") or []


def fetch_detail(session: requests.Session, congress: int, number: int | str) -> dict:
    data = _api_get(session, f"/nomination/{congress}/{number}")
    return data.get("nomination") or data


def fetch_horizons_votes(client=None) -> list[dict]:
    hz = client or get_client()
    if not hz.configured:
        return []
    try:
        resp = hz._get("/hcgi/api/congress/votes", params={"limit": VOTES_FETCH_LIMIT})
        if resp.status_code != 200:
            logger.info("Horizons votes HTTP %s — vote links will use constructed ids only", resp.status_code)
            return []
        return resp.json().get("votes") or []
    except Exception as e:
        logger.info("Horizons votes fetch failed (%s)", e)
        return []


def index_votes_by_pn(votes: list[dict]) -> dict[str, dict]:
    """Map PN citation → confirmation vote (not cloture)."""
    out: dict[str, dict] = {}
    pn_re = re.compile(r"\b(PN\d+(?:-\d+)?)\b", re.I)
    for v in votes:
        result = (v.get("result") or "").lower()
        title = f"{v.get('voteType') or ''} {v.get('title') or ''}"
        if "cloture" in title.lower() or "cloture" in result:
            continue
        if "nomination" not in title.lower() and "nomination" not in result:
            continue
        if not (
            "confirmed" in result
            or "rejected" in result
            or title.lower().startswith("on the nomination")
        ):
            continue
        for pn in pn_re.findall(title):
            key = pn.upper()
            # First confirmation-style hit wins; later cloture already skipped.
            out.setdefault(key, v)
    return out


def pick_terminal_action(actions: list[dict], latest: dict | None) -> dict | None:
    candidates = list(actions or [])
    if latest and latest.get("text"):
        candidates.append(latest)
    # Prefer an action we can map, walking newest first.
    mapped = []
    for a in candidates:
        res = map_vote_result(a.get("text"))
        if res:
            mapped.append((res, a))
    if not mapped:
        return None
    # Stable: last mapped in list is typically latestAction appended last.
    return mapped[-1][1]


def resolve_senate_vote_id(
    nomination_id: str,
    congress: int,
    action_text: str,
    vote_date: str | None,
    votes_by_pn: dict[str, dict],
) -> tuple[str | None, str]:
    roll, note = classify_vote_link(action_text)
    hz = votes_by_pn.get(nomination_id.upper())
    if hz and hz.get("id"):
        return hz["id"], "linked"
    if roll is not None:
        constructed = senate_vote_id_for(congress, vote_date, roll)
        # If we have a Horizons list, require the id to be in it.
        known_ids = {v.get("id") for v in votes_by_pn.values()}
        if known_ids and constructed not in known_ids:
            # The constructed id may still be correct but not in the 250-vote
            # window. Keep it — it is the system's join key, not a guess.
            return constructed, "roll_call_constructed"
        return constructed, "linked"
    return None, note


def map_nomination(
    item: dict,
    actions: list[dict],
    roster: list[dict],
    votes_by_pn: dict[str, dict],
) -> dict:
    description = item.get("description") or ""
    citation = (item.get("citation") or "").strip().upper()
    congress = int(item.get("congress") or 0)
    number = item.get("number")
    if not citation and number is not None:
        part = item.get("partNumber")
        citation = f"PN{number}" + (f"-{int(part)}" if part and str(part) not in ("0", "00", "") else "")
        citation = citation.upper()

    if not is_judicial(description) and not is_judicial(item.get("organization")):
        return {
            "_skip": True,
            "reason": "not_judicial",
            "citation": citation,
        }

    terminal = pick_terminal_action(actions, item.get("latestAction"))
    if not terminal:
        return {
            "_skip": True,
            "reason": "no_terminal_action",
            "citation": citation,
            "description": description,
        }

    result = map_vote_result(terminal.get("text"))
    vote_date = iso_date(terminal.get("actionDate"))
    if result not in RESULT_ENUM:
        return {
            "_review": True,
            "reason": "unexpected_structure",
            "notes": f"{citation} unmapped action: {terminal.get('text')!r}",
            "nomination_id": citation,
            "source_url": source_url_for(congress, number),
        }

    name, court = parse_nominee(description)
    judge = match_judge(name or "", court, roster) if name else None
    source_url = source_url_for(congress, number)
    if not citation or not source_url:
        return {
            "_review": True,
            "reason": "missing_source_url",
            "notes": "missing PN citation",
        }
    if judge is None:
        return {
            "_review": True,
            "reason": "unexpected_structure",
            "notes": (
                f"ambiguous or missing FJC match for name={name!r} "
                f"court={court!r} pn={citation}"
            ),
            "nomination_id": citation,
            "source_url": source_url,
            "name": name,
            "court": court,
        }

    vote_id, vote_note = resolve_senate_vote_id(
        citation, congress, terminal.get("text") or "", vote_date, votes_by_pn
    )
    president = (judge.get("appointing_president") or "").strip() or None
    rec = {
        "nomination_id": citation,
        "judge_id": judge["judge_id"],
        "nominating_president": president,
        "vote_date": vote_date,
        "vote_result": result,
        "senate_vote_id": vote_id,
        "senate_vote_note": vote_note,
        "source_url": source_url,
    }
    return rec


def run(
    congress: int,
    days_back: int = 14,
    dry_run: bool = False,
    max_records: int | None = None,
    client=None,
    roster: list[dict] | None = None,
    nominations: list[dict] | None = None,
    actions_by_number: dict | None = None,
    votes: list[dict] | None = None,
) -> dict:
    loaded = load_judges(client=client, roster=roster)
    since = date.today() - timedelta(days=days_back)

    session = None
    if nominations is None:
        session = _session()
        nominations = list_nominations(
            session, congress, since=since, max_records=max_records
        )
    elif max_records is not None:
        nominations = nominations[:max_records]

    if votes is None:
        votes = fetch_horizons_votes(client=client)
    votes_by_pn = index_votes_by_pn(votes)

    mapped: list[dict] = []
    review: list[dict] = []
    skipped = 0
    notes: dict[str, int] = {}
    for item in nominations:
        number = item.get("number")
        if actions_by_number is not None:
            actions = actions_by_number.get(str(number), [])
        elif session is not None and number is not None:
            try:
                actions = fetch_actions(session, congress, number)
            except Exception as e:
                logger.warning("actions fetch failed congress=%s number=%s: %s", congress, number, e)
                actions = []
            time.sleep(PAGE_DELAY_SECONDS)
        else:
            actions = []

        if session is not None and number is not None and not (item.get("description") or "").strip():
            try:
                detail = fetch_detail(session, congress, number)
                if detail.get("description"):
                    item = {**item, "description": detail["description"]}
                if detail.get("organization") and not item.get("organization"):
                    item = {**item, "organization": detail["organization"]}
                time.sleep(PAGE_DELAY_SECONDS)
            except Exception as e:
                logger.warning("detail fetch failed congress=%s number=%s: %s", congress, number, e)

        rec = map_nomination(item, actions, loaded, votes_by_pn)
        if rec.get("_skip"):
            skipped += 1
            notes[f"skip_{rec.get('reason') or 'other'}"] = (
                notes.get(f"skip_{rec.get('reason') or 'other'}", 0) + 1
            )
            continue
        if rec.get("_review"):
            review.append(rec)
            continue
        notes[rec.get("senate_vote_note") or "unset"] = (
            notes.get(rec.get("senate_vote_note") or "unset", 0) + 1
        )
        mapped.append(rec)

    logger.info(
        "nominations congress=%s found=%s judicial_mapped=%s review=%s skipped=%s vote_notes=%s",
        congress,
        len(nominations),
        len(mapped),
        len(review),
        skipped,
        notes,
    )
    for rec in mapped:
        if not rec.get("senate_vote_id"):
            logger.info(
                "null senate_vote_id pn=%s note=%s result=%s",
                rec.get("nomination_id"),
                rec.get("senate_vote_note"),
                rec.get("vote_result"),
            )

    posted = 0
    if dry_run:
        for rec in mapped[:8]:
            logger.info("dry-run nomination %s", json.dumps(rec, default=str)[:500])
        for rec in review[:5]:
            logger.info("dry-run review %s", json.dumps(rec, default=str)[:500])
    else:
        hz = client or get_client()
        if not hz.configured:
            raise RuntimeError("HORIZONS_BASE_URL and INGEST_API_KEY required")
        payload = [
            {k: v for k, v in rec.items() if k != "senate_vote_note" or True}
            for rec in mapped
        ]
        for i in range(0, len(payload), HORIZONS_BATCH_SIZE):
            hz.submit_judicial_nominations(payload[i : i + HORIZONS_BATCH_SIZE])
            posted += len(payload[i : i + HORIZONS_BATCH_SIZE])
        for rec in review:
            hz.flag_manual_review(
                filing_id=rec.get("nomination_id") or rec.get("source_url") or "unknown",
                reason=rec.get("reason") or "unexpected_structure",
                notes=rec.get("notes") or "",
            )
        hz.submit_ingestion_log(
            source="nominations",
            found=len(nominations),
            parsed=len(mapped),
            failed=len(review),
            flagged=len(review),
            note=f"congress={congress} days_back={days_back} vote_notes={notes}",
        )

    return {
        "found": len(nominations),
        "mapped": len(mapped),
        "review": len(review),
        "skipped": skipped,
        "posted": posted,
        "dry_run": dry_run,
        "congress": congress,
        "vote_notes": notes,
    }


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Judicial nomination ↔ judge linker")
    p.add_argument(
        "--congress",
        type=int,
        default=None,
        help="Congress number (default: current)",
    )
    p.add_argument("--days-back", type=int, default=14)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--max-records", type=int, default=None)
    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = build_arg_parser().parse_args(argv)
    try:
        config.require_config(dry_run=args.dry_run, source="nominations")
    except RuntimeError as e:
        logger.error(str(e))
        return 1
    try:
        result = run(
            congress=args.congress or current_congress(),
            days_back=args.days_back,
            dry_run=args.dry_run,
            max_records=args.max_records,
        )
    except (RuntimeError, ValueError) as e:
        logger.error(str(e))
        return 1
    logger.info("=== nominations summary === %s", result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
