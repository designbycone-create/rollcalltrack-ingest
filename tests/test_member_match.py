"""Member matching against a fixture roster, plus ingest 500 retry."""
from ingest.ingest_client import HorizonsIngestClient
from ingest.legislators import match_legislator, roster_from_records


def _person(bioguide, first, last, state, chamber, district=None, nickname="", middle="", party="Democrat"):
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
            "official_full": f"{first} {last}",
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
