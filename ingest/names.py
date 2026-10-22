"""Name normalization helpers for matching disclosure filer names to members.

Disclosure sources use inconsistent forms:
  House:  "Donald Sternoff Beyer", "April McClain Delaney", "Robert B. Aderholt"
  Senate: "Shelley M Capito", office "Capito, Shelley Moore (Senator)"
  DB:     often "Don Beyer", "Shelley Moore Capito", "Alma Adams"

Matching prefers precision over recall — ambiguous multi-matches return None
rather than guessing the wrong member.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_TITLES = re.compile(
    r"^(?:hon\.?|the\s+hon(?:orable)?\.?|rep\.?|sen\.?|mr\.?|mrs\.?|ms\.?|dr\.?)\s+",
    re.I,
)
# House index sometimes embeds "Dr" / "Mr" between given names ("Richard Dean Dr").
_INNER_TITLES = re.compile(
    r"\b(?:hon|honorable|rep|sen|mr|mrs|ms|dr|miss)\b\.?",
    re.I,
)
_SUFFIXES = re.compile(
    r",?\s+(?:jr\.?|sr\.?|ii|iii|iv|v|esq\.?)$",
    re.I,
)
_ROLE_PAREN = re.compile(
    r"\s*\((?:senator|candidate|former\s+senator|representative|rep\.?|sen\.?)\)\s*$",
    re.I,
)
_NON_ALNUM = re.compile(r"[^a-z0-9\s]")
_WS = re.compile(r"\s+")
_MIDDLE_INITIAL = re.compile(r"\b([a-z])\b\.?")

# USPS state codes used on House StateDst field (e.g. AL04, CA33)
STATE_CODES = {
    "AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas",
    "CA": "california", "CO": "colorado", "CT": "connecticut", "DE": "delaware",
    "FL": "florida", "GA": "georgia", "HI": "hawaii", "ID": "idaho",
    "IL": "illinois", "IN": "indiana", "IA": "iowa", "KS": "kansas",
    "KY": "kentucky", "LA": "louisiana", "ME": "maine", "MD": "maryland",
    "MA": "massachusetts", "MI": "michigan", "MN": "minnesota", "MS": "mississippi",
    "MO": "missouri", "MT": "montana", "NE": "nebraska", "NV": "nevada",
    "NH": "new hampshire", "NJ": "new jersey", "NM": "new mexico", "NY": "new york",
    "NC": "north carolina", "ND": "north dakota", "OH": "ohio", "OK": "oklahoma",
    "OR": "oregon", "PA": "pennsylvania", "RI": "rhode island", "SC": "south carolina",
    "SD": "south dakota", "TN": "tennessee", "TX": "texas", "UT": "utah",
    "VT": "vermont", "VA": "virginia", "WA": "washington", "WV": "west virginia",
    "WI": "wisconsin", "WY": "wyoming", "DC": "district of columbia",
    "AS": "american samoa", "GU": "guam", "MP": "northern mariana islands",
    "PR": "puerto rico", "VI": "virgin islands",
}
STATE_NAME_TO_CODE = {v: k for k, v in STATE_CODES.items()}


@dataclass(frozen=True)
class ParsedName:
    raw: str
    full: str                 # cleaned "First ... Last"
    first: str
    last: str
    middle_tokens: tuple[str, ...]
    # Variants useful as SQL match keys (lowercased)
    variants: tuple[str, ...]


def parse_state_district(state_dst: str | None) -> tuple[str | None, int | None]:
    """Parse House StateDst like 'AL04', 'CA33', 'AS00' → (state_code, district)."""
    if not state_dst:
        return None, None
    s = state_dst.strip().upper()
    m = re.match(r"^([A-Z]{2})(\d{2})$", s)
    if not m:
        # bare state?
        if s in STATE_CODES:
            return s, None
        return None, None
    code, dist = m.group(1), int(m.group(2))
    if code not in STATE_CODES:
        return None, None
    # 00 often means at-large / territory seat — keep 0 as district
    return code, dist


def normalize_state(state: str | None) -> str | None:
    """Return uppercase 2-letter code if recognizable, else None."""
    if not state:
        return None
    s = state.strip()
    if len(s) == 2 and s.upper() in STATE_CODES:
        return s.upper()
    key = _WS.sub(" ", s.lower())
    return STATE_NAME_TO_CODE.get(key)


def _clean_base(name: str) -> str:
    s = (name or "").strip()
    s = s.replace("“", " ").replace("”", " ").replace('"', " ")
    s = _ROLE_PAREN.sub("", s)
    s = _TITLES.sub("", s)
    s = _SUFFIXES.sub("", s)
    s = _INNER_TITLES.sub(" ", s)
    s = s.replace(".", " ")
    s = s.replace("-", " ")
    s = s.replace("'", "")
    s = _WS.sub(" ", s).strip()
    return s


def parse_filer_name(name: str, office: str | None = None) -> ParsedName:
    """Parse a disclosure filer name into matchable pieces + variants.

    If `office` is Senate-style "Last, First Middle (Senator)", that often
    has a more complete form than the short first/last fields.
    """
    raw = (name or "").strip()
    candidates_src = [raw]
    if office:
        candidates_src.append(office)

    # Prefer "Last, First ..." when present (Senate office field)
    full = raw
    for src in candidates_src:
        cleaned = _clean_base(src)
        if "," in cleaned:
            last, rest = [p.strip() for p in cleaned.split(",", 1)]
            if last and rest:
                full = f"{rest} {last}"
                break
        elif cleaned:
            full = cleaned

    full = _clean_base(full)
    tokens = [t for t in full.split() if t]
    if not tokens:
        return ParsedName(raw=raw, full="", first="", last="", middle_tokens=(), variants=())

    first = tokens[0]
    last = tokens[-1]
    middle = tuple(tokens[1:-1]) if len(tokens) > 2 else ()

    variants: list[str] = []

    def add(v: str) -> None:
        v = _WS.sub(" ", v.strip().lower())
        if v and v not in variants:
            variants.append(v)

    add(full)
    add(f"{first} {last}")
    if middle:
        # first + middle initials + last
        initials = " ".join(m[0] for m in middle if m)
        add(f"{first} {initials} {last}")
        add(f"{first} {' '.join(middle)} {last}")
        # Hyphenated compound last (McClain Delaney ↔ McClain-Delaney).
        # Only when penultimate token is a real name part, not a middle initial.
        penult = tokens[-2]
        if len(tokens) >= 3 and len(penult) > 1:
            add(f"{first} {penult}-{last}")
            add(f"{first} {penult} {last}")

    # Nickname-ish shortenings for common first names are NOT applied —
    # too easy to false-positive. Rely on first-token prefix SQL instead.

    # "Last, First" form
    add(f"{last}, {first}")
    if middle:
        add(f"{last}, {first} {' '.join(middle)}")

    # Alphanumeric-only compact key (no spaces/punct) for loose compare
    compact = _NON_ALNUM.sub("", full.lower())
    add(compact)

    return ParsedName(
        raw=raw,
        full=full,
        first=first,
        last=last,
        middle_tokens=middle,
        variants=tuple(variants),
    )


# Groups are united if they share a spelling, so each token maps to one set.
_NICKNAME_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"josh", "joshua"}),
    frozenset({"ro", "rohit"}),
    frozenset({"rob", "robert", "bob", "bobby", "robbie"}),
    frozenset({"bill", "william", "billy", "will", "willy", "liam"}),
    frozenset({"jim", "james", "jimmy", "jamie"}),
    frozenset({"mike", "michael", "mick", "mickey"}),
    frozenset({"chris", "christopher"}),
    frozenset({"christine", "christina", "tina", "chrissy"}),
    frozenset({"tom", "thomas", "tommy"}),
    frozenset({"dan", "daniel", "danny"}),
    frozenset({"dave", "david"}),
    frozenset({"chuck", "charles", "charlie", "chas"}),
    frozenset({"dick", "richard", "rick", "rich", "ricky"}),
    frozenset({"ben", "benjamin", "benny"}),
    frozenset({"matt", "matthew"}),
    frozenset({"pat", "patrick"}),
    frozenset({"patricia", "patty", "tricia"}),
    frozenset({"tony", "anthony"}),
    frozenset({"ted", "theodore", "theo", "teddy"}),
    frozenset({"ed", "edward", "eddie", "ned"}),
    frozenset({"joe", "joseph", "joey"}),
    frozenset({"andy", "andrew", "drew"}),
    frozenset({"jeff", "jeffrey", "jeffery"}),
    frozenset({"greg", "gregory"}),
    frozenset({"steve", "steven", "stephen"}),
    frozenset({"nick", "nicholas", "nic"}),
    frozenset({"sam", "samuel", "sammy"}),
    frozenset({"alex", "alexander", "alexandra"}),
    frozenset({"don", "donald", "donnie"}),
    frozenset({"ron", "ronald", "ronnie"}),
    frozenset({"jerry", "gerald", "gerry"}),
    frozenset({"tim", "timothy", "timmy"}),
    frozenset({"terry", "terence", "terrence"}),
    frozenset({"larry", "lawrence"}),
    frozenset({"katie", "kate", "kathy", "katherine", "catherine", "kathryn", "cathy"}),
    frozenset({"beth", "elizabeth", "liz", "lizzy", "betty", "betsy", "eliza"}),
    frozenset({"debbie", "deb", "deborah"}),
    frozenset({"becky", "rebecca"}),
    frozenset({"jenny", "jen", "jennifer"}),
    frozenset({"peggy", "peg", "margaret", "maggie", "meg"}),
    frozenset({"john", "jon", "johnny", "jack"}),
    frozenset({"frank", "francis"}),
    frozenset({"fred", "frederick", "freddy", "freddie"}),
    frozenset({"hank", "henry"}),
    frozenset({"harry", "harold"}),
    frozenset({"hal", "harold"}),
    frozenset({"sue", "susan", "suzan", "suzanne", "suzy"}),
    frozenset({"abby", "abigail"}),
    frozenset({"al", "alan", "albert"}),
    frozenset({"bernie", "bernard"}),
    frozenset({"brad", "bradley"}),
    frozenset({"cindy", "cynthia"}),
    frozenset({"doug", "douglas"}),
    frozenset({"gene", "eugene"}),
    frozenset({"jake", "jacob"}),
    frozenset({"ken", "kenneth", "kenny"}),
    frozenset({"kim", "kimberly"}),
    frozenset({"marty", "martin"}),
    frozenset({"nate", "nathan", "nathaniel"}),
    frozenset({"pam", "pamela"}),
    frozenset({"pete", "peter"}),
    frozenset({"phil", "philip", "phillip"}),
    frozenset({"ray", "raymond"}),
    frozenset({"russ", "russell"}),
    frozenset({"vince", "vincent"}),
    frozenset({"walt", "walter", "wally"}),
    frozenset({"zach", "zachary", "zack"}),
    frozenset({"thad", "thaddeus"}),
    frozenset({"max", "maxwell"}),
    frozenset({"gus", "augustus"}),
    frozenset({"lou", "louis"}),
    frozenset({"scott", "scotty"}),
)

_NICKNAME_INDEX: dict[str, frozenset[str]] | None = None


def _nickname_index() -> dict[str, frozenset[str]]:
    global _NICKNAME_INDEX
    if _NICKNAME_INDEX is not None:
        return _NICKNAME_INDEX
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        pa, pb = find(a), find(b)
        if pa != pb:
            parent[pb] = pa

    for group in _NICKNAME_GROUPS:
        items = list(group)
        for other in items[1:]:
            union(items[0], other)
    clusters: dict[str, set[str]] = {}
    for name in list(parent):
        clusters.setdefault(find(name), set()).add(name)
    _NICKNAME_INDEX = {name: frozenset(group) for group in clusters.values() for name in group}
    return _NICKNAME_INDEX


def nicknames_for(first: str) -> frozenset[str]:
    """Spellings treated as the same given name (Josh/Joshua, Rohit/Ro)."""
    token = (first or "").lower().strip(".")
    if not token:
        return frozenset()
    return _nickname_index().get(token, frozenset({token}))


def first_names_compatible(a: str, b: str) -> bool:
    """True when two given names can be the same person.

    Exact match, nickname group, or a shared prefix of at least 4 letters
    (so 'Josh' does not swallow 'John', but 'Josh'/'Joshua' still match
    via the nickname group).
    """
    left = (a or "").lower().strip(".")
    right = (b or "").lower().strip(".")
    if not left or not right:
        return False
    if left == right or left in nicknames_for(right):
        return True
    short, long = (left, right) if len(left) <= len(right) else (right, left)
    return len(short) >= 4 and long.startswith(short)


def state_display_name(code: str | None) -> str:
    """'NJ' → 'New Jersey'. Empty string when the code is unknown."""
    key = (code or "").strip().upper()
    raw = STATE_CODES.get(key)
    if not raw:
        return ""
    parts = []
    for i, word in enumerate(raw.split()):
        parts.append(word if i and word in {"of"} else word.capitalize())
    return " ".join(parts)


def member_record_compatible(
    filer_name: str,
    member: dict,
    *,
    state: str | None = None,
    office: str | None = None,
) -> bool:
    """Reject lookup hits that are a different person.

    The Horizons name search is a fuzzy contains match. It has returned
    state legislators (bioguide ``LS…``) and, for ``Allen, Richard``,
    Richard McCormick. A hit must share a last name and a compatible
    given name, and must not be a LegiScan id.
    """
    parsed = parse_filer_name(filer_name, office=office)
    if not parsed.last:
        return False
    bioguide = str(member.get("bioguide_id") or "")
    member_id = str(member.get("id") or "")
    if bioguide.upper().startswith("LS") or member_id.upper().startswith("LS"):
        return False
    stored = parse_filer_name(str(member.get("name") or ""))
    if not stored.last:
        return False
    filer_last = parsed.last.lower()
    stored_last = stored.last.lower()
    stored_full = (stored.full or "").lower()
    if filer_last != stored_last and filer_last not in stored_full.split():
        return False
    if parsed.first and stored.first and not first_names_compatible(parsed.first, stored.first):
        return False
    if state:
        want = normalize_state(state)
        got = normalize_state(member.get("state"))
        if want and got and want != got:
            return False
    return True


def first_token_prefix(first: str) -> str:
    """Prefix used for 'Don' matching 'Donald' — first 3+ chars, lowercased."""
    f = (first or "").lower()
    if len(f) <= 3:
        return f
    # Use whole first name for short matches; for longer, still use full first
    # for LIKE 'donald%' style — callers decide.
    return f
