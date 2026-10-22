"""Senate roll-call ingest from senate.gov LIS XML.

rollcalltrack.com on Hostinger fetches House roll calls from clerk.house.gov
and stores them on the votes collection (``GET /hcgi/api/votes``). The same
host's ``GET /hcgi/api/sources/votes/senate`` live-fetches senate.gov and
comes back with count 0 — senate.gov appears to block that IP. This job
runs from the scheduled GitHub/Windows runner instead, which can fetch the
public XML, and posts through ``HorizonsIngestClient.submit_votes``.

Menu (one document per congress session):
  https://www.senate.gov/legislative/LIS/roll_call_lists/vote_menu_{congress}_{session}.xml
Detail (optional member positions):
  https://www.senate.gov/legislative/LIS/roll_call_votes/vote{congress}{session}/vote_{congress}_{session}_{nnnnn}.xml

Payload matches a stored House row: ``jurisdiction=federal``, empty
``state_code``, and ``chamber`` title case (House rows use ``"House"``,
these use ``"Senate"``). ``id`` / ``rc_id`` are ``s-{congress}-{year}-{roll}``
so they do not collide with House ``{congress}-{session}-{roll}`` and so
they match the nomination linker and ``/congress/senate-vote/{congress}/{year}/{roll}``.
Re-posting the same ``source_url`` (the detail XML URL) is the idempotency key.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import date, datetime, timedelta
from xml.etree import ElementTree as ET

import requests

from . import config
from .legislators import LEGISLATORS_URL
from .nominations import current_congress

logger = logging.getLogger("rollcalltrack_ingest.senate_votes")

# A browser UA. The repo's research-bot UA is what senate.gov is likely
# refusing, including from Hostinger.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

MENU_URL = (
    "https://www.senate.gov/legislative/LIS/roll_call_lists/"
    "vote_menu_{congress}_{session}.xml"
)
DETAIL_URL = (
    "https://www.senate.gov/legislative/LIS/roll_call_votes/"
    "vote{congress}{session}/vote_{congress}_{session}_{number}.xml"
)

HORIZONS_BATCH_SIZE = 25
DETAIL_DELAY_SECONDS = 0.5

_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

_CAST = {
    "yea": "yea",
    "yes": "yea",
    "aye": "yea",
    "nay": "nay",
    "no": "nay",
    "present": "present",
    "not voting": "not_voting",
    "absent": "not_voting",
}


class SenateVoteFetchError(RuntimeError):
    """senate.gov returned a redirect or a body that is not vote XML."""


def menu_url(congress: int, session: int) -> str:
    return MENU_URL.format(congress=int(congress), session=int(session))


def detail_url(congress: int, session: int, vote_number: str | int) -> str:
    return DETAIL_URL.format(
        congress=int(congress),
        session=int(session),
        number=f"{int(vote_number):05d}",
    )


def current_senate_session(today: date | None = None) -> tuple[int, int]:
    """Congress number and Senate session (1 or 2) for ``today``.

    Session 2 of a Congress starts on January 3 of the even year. January 1–2
    still belongs to the previous session (and, in an odd year, the previous
    Congress — ``current_congress`` already handles that boundary).
    """
    today = today or date.today()
    congress = current_congress(today)
    start_year = 1789 + (congress - 1) * 2
    session = 2 if today >= date(start_year + 1, 1, 3) else 1
    return congress, session


def _collapse(value: str | None) -> str:
    return " ".join((value or "").split())


def _menu_date(vote_date: str, year: str) -> str:
    text = _collapse(vote_date)
    match = re.fullmatch(r"(\d{1,2})-([A-Za-z]{3})", text)
    month = _MONTHS.get(match.group(2).lower()) if match else None
    if not match or month is None or not str(year).isdigit():
        raise ValueError(f"unparseable Senate vote date {vote_date!r} year={year!r}")
    return f"{int(year):04d}-{month:02d}-{int(match.group(1)):02d}"


def _detail_datetime(vote_date: str) -> str | None:
    text = _collapse(vote_date)
    if not text:
        return None
    try:
        parsed = datetime.strptime(text, "%B %d, %Y, %I:%M %p")
    except ValueError:
        return None
    return parsed.strftime("%Y-%m-%dT%H:%M:%S")


def _tally(text: str | None) -> int:
    raw = _collapse(text)
    if not raw:
        return 0
    return int(raw)


def parse_menu(raw: bytes) -> list[dict]:
    """Map ``<vote>`` entries onto the House vote payload shape.

    Duplicate vote numbers in one menu collapse to the first entry so a
    re-parse cannot emit two rows for the same roll.
    """
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        raise SenateVoteFetchError("Senate vote menu is not XML") from e
    if root.tag != "vote_summary":
        raise SenateVoteFetchError(f"Senate vote menu root is {root.tag}, not vote_summary")
    congress = int(_collapse(root.findtext("congress")))
    session = int(_collapse(root.findtext("session")))
    year = _collapse(root.findtext("congress_year"))
    if not year.isdigit():
        raise SenateVoteFetchError("Senate vote menu is missing congress_year")

    votes: list[dict] = []
    seen: set[str] = set()
    for node in root.findall("./votes/vote"):
        number = _collapse(node.findtext("vote_number"))
        if not number.isdigit():
            continue
        roll = str(int(number))
        rc_id = f"s-{congress}-{year}-{roll}"
        if rc_id in seen:
            continue
        seen.add(rc_id)
        title = _collapse(node.findtext("title"))
        tally = node.find("vote_tally")
        votes.append(
            {
                "id": rc_id,
                "rc_id": rc_id,
                "congress": congress,
                "chamber": "Senate",
                "session": str(session),
                "roll_number": roll,
                "bill_label": _collapse(node.findtext("issue")),
                "title": title,
                "description": title,
                "result": _collapse(node.findtext("result")),
                "vote_type": _collapse(node.findtext("question")),
                "date": _menu_date(node.findtext("vote_date") or "", year),
                "tally_yea": _tally(tally.findtext("yeas") if tally is not None else None),
                "tally_nay": _tally(tally.findtext("nays") if tally is not None else None),
                "tally_present": 0,
                "tally_not_voting": 0,
                "jurisdiction": "federal",
                "state_code": "",
                "source_url": detail_url(congress, session, number),
            }
        )
    return votes


def _normalize_cast(raw: str | None) -> str | None:
    return _CAST.get(_collapse(raw).lower())


def parse_detail(raw: bytes, summary: dict, lis_to_bioguide: dict | None = None) -> dict:
    """Add member positions and the recorded clock time.

    Yea/nay totals stay the menu ``vote_tally``. Present and not-voting are
    counted from every ``<member>``, including ones we cannot map to a
    bioguide. Unmapped members are omitted from ``member_votes`` rather than
    sent with a guessed id. Menu ``result`` is kept (the detail text is a
    longer gloss, such as "Nomination Confirmed").
    """
    lis_to_bioguide = lis_to_bioguide or {}
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        raise SenateVoteFetchError("Senate vote detail is not XML") from e
    if root.tag != "roll_call_vote":
        raise SenateVoteFetchError(f"Senate vote detail root is {root.tag}")

    out = dict(summary)
    clock = _detail_datetime(root.findtext("vote_date") or "")
    if clock:
        out["date"] = clock

    present = 0
    not_voting = 0
    members: list[dict] = []
    for node in root.findall("./members/member"):
        choice = _normalize_cast(node.findtext("vote_cast"))
        if choice == "present":
            present += 1
        elif choice == "not_voting":
            not_voting += 1
        if choice is None:
            continue
        lis_id = _collapse(node.findtext("lis_member_id")).upper()
        bioguide = lis_to_bioguide.get(lis_id) if lis_id else None
        if not bioguide:
            continue
        members.append(
            {
                "rc_id": summary["rc_id"],
                "bioguide_id": bioguide,
                "vote": choice,
                "name": _collapse(node.findtext("member_full")),
                "jurisdiction": "federal",
                "state_code": "",
            }
        )
    out["tally_present"] = present
    out["tally_not_voting"] = not_voting
    out["member_votes"] = members
    return out


def _looks_like_html(body: bytes) -> bool:
    sniff = body.lstrip()[:300].lower()
    return (
        sniff.startswith(b"<!doctype html")
        or sniff.startswith(b"<html")
        or sniff.startswith(b"<head")
    )


def fetch_xml(url: str, session: requests.Session | None = None) -> bytes:
    """GET ``url`` with a browser User-Agent.

    Redirects (3xx) and non-XML bodies are errors. A block page that answers
    200 with HTML must not be parsed as an empty vote menu.
    """
    http = session or requests.Session()
    resp = http.get(
        url,
        timeout=config.REQUEST_TIMEOUT_SECONDS,
        allow_redirects=False,
        headers={
            "User-Agent": BROWSER_USER_AGENT,
            "Accept": "application/xml,text/xml;q=0.9,*/*;q=0.8",
        },
    )
    if 300 <= resp.status_code < 400:
        location = resp.headers.get("Location") or ""
        raise SenateVoteFetchError(
            f"Senate vote URL redirected ({resp.status_code}) {location}: {url}"
        )
    if resp.status_code != 200:
        raise SenateVoteFetchError(f"Senate vote URL HTTP {resp.status_code}: {url}")
    body = resp.content or b""
    ctype = (resp.headers.get("Content-Type") or "").lower()
    if "html" in ctype or _looks_like_html(body):
        raise SenateVoteFetchError(f"Senate vote URL returned non-XML: {url}")
    try:
        ET.fromstring(body)
    except ET.ParseError as e:
        raise SenateVoteFetchError(f"Senate vote URL returned non-XML: {url}") from e
    return body


def load_lis_to_bioguide(session: requests.Session | None = None) -> dict[str, str]:
    """Map Senate LIS member ids (``S428``) to bioguide ids.

    The detail XML does not carry bioguide. congress-legislators does, on
    ``id.lis``. An empty map means member positions are skipped, not guessed.
    """
    try:
        http = session or requests.Session()
        resp = http.get(
            LEGISLATORS_URL,
            timeout=config.REQUEST_TIMEOUT_SECONDS,
            headers={"User-Agent": BROWSER_USER_AGENT},
        )
        resp.raise_for_status()
        roster = resp.json()
    except (requests.RequestException, ValueError) as e:
        logger.warning("LIS bioguide roster unavailable: %s", e)
        return {}
    out: dict[str, str] = {}
    if not isinstance(roster, list):
        return out
    for person in roster:
        ids = person.get("id") or {}
        lis_id = str(ids.get("lis") or "").strip().upper()
        bioguide = str(ids.get("bioguide") or "").strip()
        if lis_id and bioguide:
            out[lis_id] = bioguide
    return out


def _select(
    votes: list[dict],
    *,
    today: date,
    days_back: int | None,
    max_records: int | None,
) -> list[dict]:
    chosen = votes
    if days_back is not None:
        cutoff = (today - timedelta(days=days_back)).isoformat()
        chosen = [v for v in chosen if (v.get("date") or "")[:10] >= cutoff]
    chosen.sort(key=lambda v: int(v["roll_number"]), reverse=True)
    if max_records is not None:
        chosen = chosen[:max_records]
    return chosen


def run(
    *,
    congress: int | None = None,
    session: int | None = None,
    days_back: int | None = None,
    max_records: int | None = None,
    dry_run: bool = False,
    fetch_details: bool = True,
    client=None,
    today: date | None = None,
    menu_xml: bytes | None = None,
    fetcher=None,
    lis_to_bioguide: dict[str, str] | None = None,
    include_records: bool = False,
) -> dict:
    """Fetch the current (or given) session menu and submit Senate roll calls.

    ``days_back`` limits which menu rows are posted. ``None`` posts the
    whole session. The same ``rc_id`` and detail ``source_url`` are produced
    on every run, and duplicate vote numbers in the menu are dropped.
    """
    today = today or date.today()
    if congress is None or session is None:
        current_congress_n, current_session_n = current_senate_session(today)
        if congress is None:
            congress = current_congress_n
        if session is None:
            session = current_session_n

    fetch = fetcher or fetch_xml
    empty = {"found": 0, "parsed": 0, "failed": 0, "submitted": 0}

    if menu_xml is None:
        url = menu_url(congress, session)
        try:
            menu_xml = fetch(url)
        except Exception as e:
            logger.error("Senate vote menu fetch failed: %s", e)
            _log(client, dry_run, 0, 0, 1, note=str(e))
            return {**empty, "failed": 1, "error": str(e)}

    try:
        votes = parse_menu(menu_xml)
    except Exception as e:
        logger.error("Senate vote menu parse failed: %s", e)
        _log(client, dry_run, 0, 0, 1, note=str(e))
        return {**empty, "failed": 1, "error": str(e)}

    selected = _select(
        votes, today=today, days_back=days_back, max_records=max_records
    )
    if fetch_details and lis_to_bioguide is None and selected:
        lis_to_bioguide = load_lis_to_bioguide()
    lis_to_bioguide = lis_to_bioguide or {}

    records: list[dict] = []
    failed = 0
    live_details = fetcher is None and menu_xml is not None and fetch_details
    for index, rec in enumerate(selected):
        if not fetch_details:
            records.append(rec)
            continue
        if live_details and index:
            time.sleep(DETAIL_DELAY_SECONDS)
        try:
            detail_raw = fetch(rec["source_url"])
            records.append(parse_detail(detail_raw, rec, lis_to_bioguide=lis_to_bioguide))
        except Exception as e:
            logger.warning(
                "Senate vote detail skipped rc_id=%s: %s", rec.get("rc_id"), e
            )
            records.append(rec)
            failed += 1

    submitted = 0
    if dry_run:
        for rec in records[:5]:
            logger.info(
                "[dry-run] Senate vote %s %s yeas=%s nays=%s members=%s",
                rec.get("rc_id"),
                rec.get("result"),
                rec.get("tally_yea"),
                rec.get("tally_nay"),
                len(rec.get("member_votes") or []),
            )
    elif client is not None and getattr(client, "configured", True):
        for start in range(0, len(records), HORIZONS_BATCH_SIZE):
            batch = records[start : start + HORIZONS_BATCH_SIZE]
            try:
                result = client.submit_votes(batch) or {}
                if isinstance(result, dict) and result.get("submitted") is not None:
                    submitted += int(result["submitted"])
                elif isinstance(result, dict) and result.get("written") is not None:
                    submitted += int(result["written"])
            except Exception as e:
                failed += len(batch)
                logger.error("Senate vote submit failed: %s", e)
        _log(
            client,
            dry_run,
            len(selected),
            len(records),
            failed,
            note=f"senate.gov LIS congress={congress} session={session}",
        )
    elif not dry_run:
        logger.info("Senate votes parsed but no Horizons client configured; not posting")

    result = {
        "found": len(selected),
        "parsed": len(records),
        "failed": failed,
        "submitted": submitted,
    }
    # The daily job logs this dict. Member positions stay out unless a
    # caller (tests) asks for them.
    if include_records:
        result["records"] = records
    return result


def _log(client, dry_run: bool, found: int, parsed: int, failed: int, note: str = "") -> None:
    if dry_run or client is None or not getattr(client, "configured", True):
        return
    try:
        client.submit_ingestion_log(
            "senate_votes", found, parsed, failed, note=note
        )
    except Exception as e:
        logger.warning("senate_votes ingestion log failed: %s", e)
