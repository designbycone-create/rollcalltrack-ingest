"""Member matching against a fixture roster, plus ingest 500 retry."""
from ingest.ingest_client import HorizonsIngestClient
from ingest.legislators import match_legislator, roster_from_records


def _person(
    bioguide,
    first,
    last,
    state,
    chamber,
    district=None,
    nickname="",
    middle="",
    party="Democrat",
    official_full=None,
):
    term = {"type": "rep" if chamber == "house" else "sen", "state": state, "party": party}
    if district is not None:
        term["district"] = district
    return {
        "id": {"bioguide": bioguide},
        "name": {
            "first": first,
            "last": last,
            "middle": middle,
            "nickname": nickname,
            "official_full": official_full or f"{first} {last}",
        },
        "terms": [term],
    }


ROSTER = roster_from_records(
    [
        _person("G000583", "Josh", "Gottheimer", "NJ", "house", 5),
        _person("K000389", "Ro", "Khanna", "CA", "house", 17),
        _person("A000372", "Rick", "Allen", "GA", "house", 12, middle="W."),
        _person("M001218", "Richard", "McCormick", "GA", "house", 6, nickname="Rich"),
        _person("T000278", "Tommy", "Tuberville", "AL", "senate"),
        _person("P000595", "Gary", "Peters", "MI", "senate", middle="C."),
        _person("S000001", "John", "Smith", "TX", "house", 1),
        _person("S000002", "John", "Smith", "TX", "house", 2),
        _person(
            "M001232",
            "April",
            "McClain Delaney",
            "MD",
            "house",
            6,
            middle="Lynn",
        ),
    ]
)


def test_gottheimer_nickname_and_district():
    hit = match_legislator(
        "Joshua Gottheimer",
        ROSTER,
        state="NJ",
        district=5,
        chamber="house",
    )
    assert hit["bioguide"] == "G000583"
    short = match_legislator("Josh Gottheimer", ROSTER, state_district="NJ05", chamber="house")
    assert short["bioguide"] == "G000583"
    assert short["party"] == "D"


def test_khanna_legal_name():
    hit = match_legislator("Rohit Khanna", ROSTER, state="CA", district=17, chamber="house")
    assert hit["bioguide"] == "K000389"


def test_allen_not_mccormick():
    hit = match_legislator(
        "Richard W. Allen", ROSTER, state="GA", district=12, chamber="house"
    )
    assert hit["bioguide"] == "A000372"
    other = match_legislator(
        "Richard Dean Dr McCormick", ROSTER, state="GA", district=6, chamber="house"
    )
    assert other["bioguide"] == "M001218"


def test_senate_nicknames_and_middle_initial():
    assert match_legislator("Thomas H Tuberville", ROSTER, chamber="senate")["bioguide"] == "T000278"
    assert match_legislator("Gary C Peters", ROSTER, chamber="senate")["bioguide"] == "P000595"


def test_compound_surname_ignores_false_middle():
    hit = match_legislator(
        "April McClain Delaney", ROSTER, state="MD", district=6, chamber="house"
    )
    assert hit["bioguide"] == "M001232"


def test_unique_name_survives_stale_district():
    # Clerk StateDst can disagree with the roster after redistricting.
    roster = roster_from_records(
        [_person("M001218", "Rich", "McCormick", "GA", "house", 7, middle="Dean")]
    )
    hit = match_legislator(
        "Richard Dean Dr McCormick", roster, state="GA", district=6, chamber="house"
    )
    assert hit["bioguide"] == "M001218"


def test_senate_failures_match_congress_legislators():
    """Names from disclosure ingest run 39795750445.

    congress-legislators stores Mitch / Bernie / Richard / John, plus
    official_full. The filing forms are the legal name, a nickname, or an
    all-caps paper row whose office is only the title Senator.
    """
    roster = roster_from_records(
        [
            _person(
                "M000355",
                "Mitch",
                "McConnell",
                "KY",
                "senate",
                party="Republican",
                official_full="Mitch McConnell",
            ),
            _person(
                "B001277",
                "Richard",
                "Blumenthal",
                "CT",
                "senate",
                official_full="Richard Blumenthal",
            ),
            _person(
                "M001242",
                "Bernie",
                "Moreno",
                "OH",
                "senate",
                party="Republican",
                official_full="Bernie Moreno",
            ),
            _person(
                "B001236",
                "John",
                "Boozman",
                "AR",
                "senate",
                party="Republican",
                official_full="John Boozman",
            ),
            _person(
                "A000001",
                "Mitch",
                "McConnell",
                "KY",
                "senate",
                official_full="Addison Mitchell McConnell",
            ),
        ]
    )
    # Two McConnells would be ambiguous. Use the live shape: one person,
    # official_full "Mitch McConnell", filing "A. Mitchell McConnell, Jr."
    live = [row for row in roster if row["bioguide"] != "A000001"]
    hit = match_legislator(
        "A. Mitchell McConnell, Jr.",
        live,
        office="McConnell, A. Mitchell Jr. (Senator)",
        chamber="senate",
    )
    assert hit["bioguide"] == "M000355"

    # official_full carries the legal name when the roster first name is the nickname.
    legal = [row for row in roster if row["bioguide"] == "A000001"]
    by_official = match_legislator(
        "Addison McConnell",
        legal,
        chamber="senate",
    )
    assert by_official["bioguide"] == "A000001"

    blumenthal = match_legislator(
        "RICHARD  BLUMENTHAL",
        live,
        office="Senator",
        chamber="senate",
    )
    assert blumenthal["bioguide"] == "B001277"

    moreno = match_legislator(
        "Bernie Moreno",
        live,
        office="Moreno, Bernardo (Senator)",
        chamber="senate",
    )
    assert moreno["bioguide"] == "M001242"

    # Run 39795750445 looked Boozman up with state Arkansas and had already
    # hit earlier in the same job. The later miss was HTTP 429 plus
    # RemoteDisconnected, not a full-name versus USPS-code mismatch.
    boozman = match_legislator(
        "John Boozman",
        live,
        state="Arkansas",
        office="Boozman, John (Senator)",
        chamber="senate",
    )
    assert boozman["bioguide"] == "B001236"
    assert boozman["state"] == "AR"


def test_unique_chamber_last_name_accepts_middle_or_nickname():
    """'Robert Mitchell' fails a first-name check against 'Mitch'.

    Mitchell is the middle name, Mitch is the nickname, and the Senate
    roster has one McConnell.
    """
    roster = roster_from_records(
        [
            _person(
                "M000355",
                "Mitch",
                "McConnell",
                "KY",
                "senate",
                official_full="Mitch McConnell",
            )
        ]
    )
    hit = match_legislator(
        "Robert Mitchell McConnell",
        roster,
        chamber="senate",
    )
    assert hit["bioguide"] == "M000355"
    # A different given name, with no middle/nickname overlap, stays unmatched.
    assert match_legislator("Susan McConnell", roster, chamber="senate") is None
    # State on the filing still has to agree.
    assert (
        match_legislator(
            "Robert Mitchell McConnell", roster, state="TX", chamber="senate"
        )
        is None
    )
    two = roster_from_records(
        [
            _person("M000355", "Mitch", "McConnell", "KY", "senate"),
            _person("M000999", "Addison", "McConnell", "TX", "senate"),
        ]
    )
    assert match_legislator("Robert Mitchell McConnell", two, chamber="senate") is None


def test_ambiguous_same_name_returns_none():
    assert match_legislator("John Smith", ROSTER, state="TX", chamber="house") is None
    assert match_legislator("John Smith", ROSTER, state="TX", district=2, chamber="house")["bioguide"] == "S000002"


class _Resp:
    def __init__(self, status, data):
        self.status_code = status
        self._data = data
        self.text = str(data)

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


def test_lookup_tries_full_name_then_last_name_then_bioguide(monkeypatch):
    """Filings store USPS codes. Lookup only matches full state names."""
    hz = HorizonsIngestClient("https://example.test", "not-a-real-key")
    calls = []

    def fake_lookup(**params):
        calls.append(params)
        if params.get("bioguide_id") == "T000490":
            return {
                "id": "pb-taylor",
                "bioguide_id": "T000490",
                "name": "David Taylor",
                "state": "Ohio",
                "party": "R",
            }
        return None

    monkeypatch.setattr(hz, "_lookup_http", fake_lookup)
    monkeypatch.setattr(
        "ingest.ingest_client.match_legislator",
        lambda *args, **kwargs: {
            "bioguide": "T000490",
            "state": "OH",
            "party": "R",
            "official_full": "David Taylor",
            "chamber": "house",
        },
    )
    member = hz.lookup_member_by_name(
        "David J. Taylor",
        state="OH",
        chamber="house",
        bioguide_id="T000490",
    )
    assert member["id"] == "pb-taylor"
    assert calls == [
        {"name": "David Taylor", "state": "Ohio", "chamber": "house"},
        {"name": "Taylor", "state": "Ohio", "chamber": "house"},
        {"bioguide_id": "T000490", "chamber": "house"},
    ]
    assert all("district" not in call for call in calls)


def test_lookup_stops_when_full_name_and_state_match(monkeypatch):
    hz = HorizonsIngestClient("https://example.test", "not-a-real-key")
    calls = []

    def fake_lookup(**params):
        calls.append(params)
        return {
            "id": "pb-rich",
            "bioguide_id": "M001218",
            "name": "Richard McCormick",
            "state": "Georgia",
            "party": "R",
        }

    monkeypatch.setattr(hz, "_lookup_http", fake_lookup)
    monkeypatch.setattr(
        "ingest.ingest_client.match_legislator",
        lambda *args, **kwargs: {
            "bioguide": "M001218",
            "state": "GA",
            "party": "R",
            "official_full": "Richard McCormick",
        },
    )
    member = hz.lookup_member_by_name(
        "Richard Dean Dr McCormick",
        state_district="GA06",
        chamber="house",
    )
    assert member["id"] == "pb-rich"
    assert member["party"] == "R"
    assert calls == [{"name": "Richard McCormick", "state": "Georgia", "chamber": "house"}]


def test_lookup_uses_bioguide_when_state_on_the_filing_is_wrong(monkeypatch):
    # Scott Peters is CA (P000608). A filing stored as MI must not stick
    # on Gary Peters once the bioguide is known.
    hz = HorizonsIngestClient("https://example.test", "not-a-real-key")

    def fake_lookup(**params):
        if params.get("state") == "Michigan":
            return {
                "id": "pb-gary",
                "bioguide_id": "P000595",
                "name": "Gary Peters",
                "state": "Michigan",
                "party": "D",
            }
        if params.get("bioguide_id") == "P000608":
            return {
                "id": "pb-scott",
                "bioguide_id": "P000608",
                "name": "Scott Peters",
                "state": "California",
                "party": "D",
            }
        return None

    monkeypatch.setattr(hz, "_lookup_http", fake_lookup)
    monkeypatch.setattr("ingest.ingest_client.match_legislator", lambda *a, **k: None)
    member = hz.lookup_member_by_name(
        "Scott H. Peters",
        state="MI",
        bioguide_id="P000608",
        chamber="house",
    )
    assert member["id"] == "pb-scott"
    assert member["bioguide_id"] == "P000608"


def test_lookup_does_not_invent_a_member_id_from_the_roster(monkeypatch):
    hz = HorizonsIngestClient("https://example.test", "not-a-real-key")
    monkeypatch.setattr(hz, "_lookup_http", lambda **params: None)
    monkeypatch.setattr(
        "ingest.ingest_client.match_legislator",
        lambda *a, **k: {"bioguide": "L000597", "state": "FL", "party": "R"},
    )
    assert (
        hz.lookup_member_by_name("Laurel M. Lee", state="FL", chamber="house") is None
    )


def test_filing_500_retries_without_member_id(monkeypatch):
    hz = HorizonsIngestClient("https://example.test", "not-a-real-key")
    seen = []

    def fake_post(body, path="/hcgi/api/ingest/disclosures"):
        filing = body["payload"]["filing"]
        seen.append(filing.get("member_id"))
        if len(seen) == 1:
            return _Resp(500, {"message": "Something went wrong!"})
        assert filing.get("bioguide_id") == "G000583"
        assert filing.get("member_party") == "D"
        return _Resp(200, {"filing_id": "abc", "transactions_written": 1})

    monkeypatch.setattr(hz, "_post_ingest", fake_post)
    result = hz.submit_filing(
        {
            "source_url": "https://example.test/ptr.pdf",
            "member_id": "G000583",
            "bioguide_id": "G000583",
            "member_party": "D",
            "member_name": "Josh Gottheimer",
        },
        transactions=[{"asset_name": "Apple Inc. (AAPL)", "transaction_type": "purchase"}],
    )
    assert seen == ["G000583", None]
    assert result["filing_id"] == "abc"
