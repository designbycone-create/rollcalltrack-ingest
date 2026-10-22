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
    r"^(?:hon\.?|the\s+hon(?:orable)?\.?|rep\.?|sen\.?|senator|representative|"
    r"congress(?:man|woman)|mr\.?|mrs\.?|ms\.?|dr\.?)\s+",
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
# An eFD office cell is sometimes only the chamber title ("Senator").
# That is not a person's name.
_BARE_OFFICE_WORDS = frozenset({
    "senator", "senators", "representative", "representatives",
    "congressman", "congresswoman", "rep", "sen", "hon", "honorable",
    "member", "candidate",
})
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
    # "Dr. Hon. Jane Public III" carries more than one title or suffix.
    previous = None
    while previous != s:
        previous = s
        s = _TITLES.sub("", s)
        s = _SUFFIXES.sub("", s)
        s = _INNER_TITLES.sub(" ", s)
        s = _WS.sub(" ", s).strip()
    s = s.replace(".", " ")
    s = s.replace("-", " ")
    s = s.replace("'", "")
    s = _WS.sub(" ", s).strip()
    previous = None
    while previous != s:
        previous = s
        s = _SUFFIXES.sub("", s).strip()
        s = _TITLES.sub("", s).strip()
    return _normalize_case(s)


def _normalize_case(name: str) -> str:
    """Title-case shouted tokens. Leave McConnell-style mixed case alone."""
    parts = []
    for token in name.split():
        letters = re.sub(r"[^A-Za-z]", "", token)
        if len(letters) > 1 and letters.isupper():
            parts.append(token.title())
        else:
            parts.append(token)
    return " ".join(parts)


def is_bare_office_title(text: str | None) -> bool:
    """True when ``text`` is only a chamber title, not a filer name."""
    raw = _ROLE_PAREN.sub("", text or "")
    raw = raw.replace(".", " ")
    tokens = [t.strip(".").lower() for t in _WS.sub(" ", raw).split() if t.strip(".")]
    if not tokens:
        return True
    return all(t in _BARE_OFFICE_WORDS for t in tokens)


def _given_name_tokens(parsed: ParsedName) -> list[str]:
    """Given-name tokens long enough to compare. Skips a lone initial."""
    tokens: list[str] = []

    def add(value: str | None) -> None:
        text = (value or "").strip(".")
        if len(text) <= 1:
            return
        if text not in tokens:
            tokens.append(text)

    add(preferred_given_name(parsed))
    add(parsed.first)
    for mid in parsed.middle_tokens:
        add(mid)
    return tokens


def preferred_given_name(parsed: ParsedName) -> str:
    """Given name to match and to send to /members/lookup.

    ``A. Mitchell McConnell`` parses the initial as the first name. The
    following token is the name he uses (Mitchell → Mitch).
    """
    first = (parsed.first or "").strip(".")
    if len(first) == 1:
        for mid in parsed.middle_tokens:
            token = mid.strip(".")
            if len(token) > 1:
                return mid
    return parsed.first


def state_for_member_lookup(state: str | None) -> str | None:
    """Full state name for ``/members/lookup``.

    The live roster stores ``California`` and ``Ohio``. A filing's 2-letter
    code (``CA``, ``OH``, ``DC``, ``PR``) does not match that field.
    """
    code = normalize_state(state)
    if not code:
        return None
    return state_display_name(code) or None


def normalized_lookup_names(name: str, office: str | None = None) -> tuple[str, str]:
    """``(First Last, Last)`` with titles, suffixes, and middle names removed.

    House Clerk rows look like ``Richard Dean Dr McCormick``. The lookup
    that matches is ``Richard McCormick``, then ``McCormick``.
    """
    parsed = parse_filer_name(name, office=office)
    given = preferred_given_name(parsed)
    if is_bare_office_title(given) or is_bare_office_title(parsed.last):
        return "", ""
    full = " ".join(part for part in (given, parsed.last) if part)
    return full, parsed.last


def parse_filer_name(name: str, office: str | None = None) -> ParsedName:
    """Parse a disclosure filer name into matchable pieces + variants.

    If `office` is Senate-style "Last, First Middle (Senator)", that often
    has a more complete form than the short first/last fields.
    """
    raw = (name or "").strip()

    def from_comma(cleaned: str) -> str | None:
        if "," not in cleaned:
            return None
        last, rest = [part.strip() for part in cleaned.split(",", 1)]
        if not last or not rest:
            return None
        if is_bare_office_title(last) or is_bare_office_title(rest):
            return None
        return _clean_base(f"{rest} {last}")

    # The filer name wins unless the office cell is "Last, First (Senator)",
    # which is often more complete. A bare "Senator" is not a name.
    full = ""
    if raw and not is_bare_office_title(raw):
        cleaned = _clean_base(raw)
        full = from_comma(cleaned) or cleaned
    if office and not is_bare_office_title(office):
        office_cleaned = _clean_base(office)
        office_full = from_comma(office_cleaned)
        if office_full:
            full = office_full

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
    # A. Mitchell McConnell is filed under the middle name. Mitch is that name,
    # not a shortening of Michael.
    frozenset({"mitch", "mitchell"}),
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
    frozenset({"bernie", "bernard", "bernardo"}),
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


def state_from_office(office: str | None) -> str | None:
    """USPS code embedded in an eFD office line, if there is one.

    Senate rows often look like ``McConnell, A. Mitchell Jr. (Senator)`` and
    carry no state. Some say ``Senator (KY)`` or name the state in words.
    """
    if not office:
        return None
    text = f" {_WS.sub(' ', office.lower())} "
    for name, code in sorted(
        ((name, code) for code, name in STATE_CODES.items()),
        key=lambda item: len(item[0]),
        reverse=True,
    ):
        if f" {name} " in text:
            return code
    for match in re.finditer(r"\(([A-Za-z]{2})\)", office):
        code = match.group(1).upper()
        if code in STATE_CODES:
            return code
    return None


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
    if not parsed.last or is_bare_office_title(parsed.last):
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
    # "A. Mitchell" fails a first-initial check against "Mitch". A longer
    # middle token or nickname (Mitchell → Mitch) is enough.
    filer_tokens = _given_name_tokens(parsed)
    stored_givens = [stored.first, *stored.middle_tokens]
    if filer_tokens and stored.first and not any(
        first_names_compatible(token, other)
        for token in filer_tokens
        for other in stored_givens
        if other
    ):
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
