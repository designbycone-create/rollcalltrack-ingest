"""Match disclosure filers to current members of Congress.

The Horizons ``/members/lookup`` name search misses filers when the client
sends a 2-letter state (the live roster stores "New Jersey") and it sometimes
returns a different person. This module resolves a bioguide from the public
congress-legislators roster first, then the caller looks that id up exactly.

Source: https://unitedstates.github.io/congress-legislators/legislators-current.json
(unitedstates/congress-legislators, public domain).
"""
from __future__ import annotations

import logging
from typing import Any

import requests

from .names import (
    first_names_compatible,
    normalize_state,
    parse_filer_name,
    parse_state_district,
    preferred_given_name,
)

logger = logging.getLogger("rollcalltrack_ingest.legislators")

LEGISLATORS_URL = (
    "https://unitedstates.github.io/congress-legislators/legislators-current.json"
)
# Former members, including senators who left mid-Congress. Lindsey Graham
# (S293) and Markwayne Mullin (S419) are here, not in the current file:
# their terms in this dataset end 2026-07-11 and 2026-03-23. Rubio (S350)
# and Vance (S421) resigned in January 2025 and are here too.
LEGISLATORS_HISTORICAL_URL = (
    "https://unitedstates.github.io/congress-legislators/"
    "legislators-historical.json"
)

_CACHE: dict[str, Any] = {"roster": None, "failed": False}


def party_letter(raw: str | None) -> str:
    s = (raw or "").strip().lower()
    if s in {"d", "r", "i", "n"}:
        return s.upper()
    if s.startswith("dem"):
        return "D"
    if s.startswith("rep"):
        return "R"
    if s.startswith("ind"):
        return "I"
    return ""


def _chamber(term: dict) -> str | None:
    kind = (term.get("type") or "").lower()
    if kind == "rep":
        return "house"
    if kind == "sen":
        return "senate"
    return None


def roster_from_records(records: list[dict]) -> list[dict]:
    """Normalize congress-legislators person objects to match records."""
    out: list[dict] = []
    for person in records:
        terms = person.get("terms") or []
        if not terms:
            continue
        term = terms[-1]
        chamber = _chamber(term)
        if chamber is None:
            continue
        name = person.get("name") or {}
        last = (name.get("last") or "").strip()
        first = (name.get("first") or "").strip()
        if not last or not first:
            continue
        bioguide = ((person.get("id") or {}).get("bioguide") or "").strip()
        if not bioguide:
            continue
        district = term.get("district")
        try:
            district = int(district) if district is not None else None
        except (TypeError, ValueError):
            district = None
        out.append(
            {
                "bioguide": bioguide,
                "first": first,
                "middle": (name.get("middle") or "").strip(),
                "nickname": (name.get("nickname") or "").strip(),
                "last": last,
                "suffix": (name.get("suffix") or "").strip(),
                "official_full": (name.get("official_full") or f"{first} {last}").strip(),
                "state": (term.get("state") or "").strip().upper(),
                "district": district,
                "chamber": chamber,
                "party": party_letter(term.get("party")),
            }
        )
    return out


def load_current_legislators(
    session: requests.Session | None = None,
    *,
    force: bool = False,
) -> list[dict]:
    """Download the current roster once per process. Empty list on failure."""
    if not force and _CACHE["roster"] is not None:
        return _CACHE["roster"]
    if not force and _CACHE["failed"]:
        return []
    try:
        sess = session or requests.Session()
        resp = sess.get(LEGISLATORS_URL, timeout=30)
        resp.raise_for_status()
        roster = roster_from_records(resp.json())
    except (requests.RequestException, ValueError, TypeError) as e:
        logger.warning("congress-legislators roster unavailable: %s", e)
        _CACHE["failed"] = True
        return []
    if not roster:
        logger.warning("congress-legislators roster parsed empty")
        _CACHE["failed"] = True
        return []
    logger.info("Loaded %d current legislators for member matching", len(roster))
    _CACHE["roster"] = roster
    _CACHE["failed"] = False
    return roster


def _last_compatible(parsed, member: dict) -> bool:
    leg_last = (member.get("last") or "").lower()
    if not leg_last or not parsed.last:
        return False
    filer_last = parsed.last.lower()
    if filer_last == leg_last:
        return True
    tokens = [t.lower() for t in (parsed.full or "").split()]
    if len(tokens) >= 2 and " ".join(tokens[-2:]) == leg_last:
        return True
    if len(tokens) >= 3 and " ".join(tokens[-3:]) == leg_last:
        return True
    # Filer used only the final piece of a compound surname.
    return filer_last == leg_last.split()[-1] and len(leg_last.split()) > 1 and (
        len(tokens) >= 2 and tokens[-2] == leg_last.split()[0]
    )


def _member_given_names(member: dict) -> list[str]:
    """First name, nickname, and the given tokens of official_full."""
    names = [member.get("first") or "", member.get("nickname") or ""]
    official = parse_filer_name(member.get("official_full") or "")
    if official.first:
        names.append(official.first)
        names.append(preferred_given_name(official))
    names.extend(official.middle_tokens)
    return [name for name in names if name]


def _first_compatible(parsed, member: dict) -> bool:
    given = preferred_given_name(parsed)
    if not given:
        return False
    return any(
        first_names_compatible(given, opt) for opt in _member_given_names(member)
    )


def _given_middle_tokens(parsed, member: dict) -> tuple[str, ...]:
    """Middle tokens that are not the front of a compound surname.

    'April McClain Delaney' parses McClain as a middle token, but the
    roster stores last='McClain Delaney' and middle='Lynn'.
    """
    tokens = list(parsed.middle_tokens)
    parts = (member.get("last") or "").split()
    if len(parts) > 1 and tokens:
        prefix = [p.lower() for p in parts[:-1]]
        lowered = [t.lower() for t in tokens]
        if lowered[-len(prefix) :] == prefix:
            tokens = tokens[: -len(prefix)]
    return tuple(tokens)


def _middle_compatible(parsed, member: dict) -> bool:
    """Reject when both sides have a middle name and the initials differ."""
    middle = (member.get("middle") or "").strip(".")
    middles = _given_middle_tokens(parsed, member)
    # "A. Mitchell" uses Mitchell as the given name, not as a middle name
    # that should disagree with the roster's middle initial.
    given = preferred_given_name(parsed)
    if parsed.first and len(parsed.first.strip(".")) == 1 and given:
        middles = tuple(
            token
            for token in middles
            if token.strip(".").lower() != given.strip(".").lower()
        )
    if not middle or not middles:
        return True
    filer_mid = middles[0].strip(".")
    if not filer_mid:
        return True
    # Ignore leftover title-like tokens already stripped by the parser.
    if len(filer_mid) == 1:
        return filer_mid.lower() == middle[0].lower()
    if len(middle) == 1:
        return middle.lower() == filer_mid[0].lower()
    return filer_mid.lower() == middle.lower() or first_names_compatible(filer_mid, middle)


def _nickname_or_middle_compatible(parsed, member: dict) -> bool:
    """True when a middle name or nickname lines up, even if the first initial does not."""
    tokens: list[str] = []
    given = preferred_given_name(parsed)
    if given and len(given.strip(".")) > 1:
        tokens.append(given.strip("."))
    for mid in parsed.middle_tokens:
        text = mid.strip(".")
        if len(text) > 1 and text not in tokens:
            tokens.append(text)
    options = _member_given_names(member)
    middle = (member.get("middle") or "").strip()
    if middle:
        options.append(middle)
    return any(
        first_names_compatible(token, option)
        for token in tokens
        for option in options
        if option
    )


def _unique_last_name_in_chamber(parsed, roster, chamber_l, state_code):
    """One person in the chamber shares this last name and a middle/nickname.

    ``A. Mitchell McConnell`` fails ``A`` vs ``Mitch``. Mitchell is the name
    he uses, and the Senate has one McConnell.
    """
    pool = []
    for member in roster:
        if chamber_l and member.get("chamber") != chamber_l:
            continue
        if not _last_compatible(parsed, member):
            continue
        pool.append(member)
    if len(pool) != 1:
        return None
    member = pool[0]
    if state_code and member.get("state") and member["state"] != state_code:
        return None
    if not _nickname_or_middle_compatible(parsed, member):
        return None
    return member


def match_legislator(
    name: str,
    roster: list[dict] | None = None,
    *,
    state: str | None = None,
    district: int | None = None,
    chamber: str | None = None,
    office: str | None = None,
    state_district: str | None = None,
) -> dict | None:
    """Return one legislator dict, or None when nobody or several people fit.

    Ambiguous matches return None on purpose. Guessing the wrong member is
    worse than leaving party blank.
    """
    if state_district and (state is None or district is None):
        code, dist = parse_state_district(state_district)
        state = state or code
        if district is None:
            district = dist
    if roster is None:
        roster = load_current_legislators()
    parsed = parse_filer_name(name, office=office)
    if not parsed.last:
        return None
    state_code = normalize_state(state)
    chamber_l = (chamber or "").lower() or None

    hits: list[dict] = []
    for member in roster:
        if chamber_l and member.get("chamber") != chamber_l:
            continue
        if state_code and member.get("state") and member["state"] != state_code:
            continue
        if not _last_compatible(parsed, member):
            continue
        if not _first_compatible(parsed, member):
            continue
        if not _middle_compatible(parsed, member):
            continue
        hits.append(member)

    if district is not None:
        same_district = []
        for member in hits:
            if member.get("district") is None:
                continue
            try:
                if int(member["district"]) == int(district):
                    same_district.append(member)
            except (TypeError, ValueError):
                continue
        # District breaks ties. A unique name in the state still matches when
        # the roster district lags the Clerk's StateDst (redistricting).
        if len(same_district) == 1:
            return same_district[0]
        if len(same_district) > 1:
            hits = same_district

    if len(hits) == 1:
        return hits[0]
    if not hits:
        return _unique_last_name_in_chamber(parsed, roster, chamber_l, state_code)

    def exact_first(rows: list[dict]) -> list[dict]:
        want = parsed.first.lower()
        return [row for row in rows if (row.get("first") or "").lower() == want]

    narrowed = exact_first(hits)
    if len(narrowed) == 1:
        return narrowed[0]
    # District already applied. Still ambiguous.
    logger.info(
        "Ambiguous legislator match for %r state=%s district=%s: %s",
        name,
        state_code,
        district,
        [(h.get("bioguide"), h.get("official_full")) for h in hits[:5]],
    )
    return None
