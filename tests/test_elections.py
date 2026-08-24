"""Offline tests for elections catalog, FEC mapping, and state race slugs."""
from ingest.elections_catalog import (
    CATALOG_PATH,
    is_official_host,
    load_catalog,
    run as run_catalog,
    validate_row,
)
from ingest.fec_elections import map_party, map_totals_row, race_slug
from ingest.elections_state_races import governor_race, race_from_member
from ingest.legiscan import STATE_CODES


def test_catalog_has_federal_and_fifty_states():
    rows = load_catalog()
    assert CATALOG_PATH.is_file()
    assert len(rows) == 51
    federal = [r for r in rows if r.get("jurisdiction") == "federal"]
    states = [r for r in rows if r.get("jurisdiction") == "state"]
    assert len(federal) == 1
    assert federal[0]["general_date"] == "2026-11-03"
    codes = [r["state_code"] for r in states]
    assert set(codes) == set(STATE_CODES)


def test_every_row_has_official_source_and_portal():
    for rec in load_catalog():
        assert rec["source_url"].startswith("http")
        assert rec["sos_portal_url"].startswith("http")
        assert is_official_host(rec["source_url"]), rec["source_url"]
        validate_row(rec)


def test_fec_party_and_slug():
    assert map_party("DEM") == "D"
    assert map_party("REP") == "R"
    assert map_party("LIB") == "O"
    assert race_slug(2026, "S", "TX", None, False) == "2026-tx-us-senate"
    assert race_slug(2026, "H", "TX", 21, False) == "2026-tx-us-house-21"
    assert race_slug(2026, "H", "TX", 18, True) == "2026-tx-us-house-18-special"
    assert race_slug(2026, "H", "AK", 0, False) == "2026-ak-us-house-1"


def test_map_totals_row_senate_incumbent():
    row = {
        "office": "S",
        "state": "GA",
        "candidate_id": "S8GA00180",
        "name": "OSSOFF, T. JONATHAN",
        "party": "DEM",
        "district_number": 0,
        "incumbent_challenge": "I",
        "receipts": "97986263.04",
        "disbursements": 59730545.53,
        "cash_on_hand_end_period": "42587451.00",
        "coverage_end_date": "2026-06-30",
        "candidate_inactive": False,
    }
    race, cand = map_totals_row(row, 2026)
    assert race["slug"] == "2026-ga-us-senate"
    assert race["office"] == "us_senate"
    assert race["is_term_up"] is True
    assert cand["party"] == "D"
    assert cand["is_incumbent"] is True
    assert cand["receipts"] == 97986263.04
    assert cand["source_url"].endswith("S8GA00180/")


def test_state_race_from_member():
    sos = {"TX": "https://www.sos.state.tx.us/elections/index.shtml"}
    rec = race_from_member(
        {
            "state_code": "TX",
            "chamber": "house",
            "district": 21,
            "name": "Jane Doe",
            "bioguide_id": "LS1",
            "source_url": "https://legiscan.com/TX/people/jane-doe/id/1",
        },
        2026,
        sos,
    )
    assert rec["slug"] == "2026-tx-hd-21"
    assert rec["office"] == "state_house"
    assert rec["is_term_up"] is True
    senate = race_from_member(
        {
            "state_code": "TX",
            "chamber": "senate",
            "district": 3,
            "name": "John Doe",
            "bioguide_id": "LS2",
            "source_url": "https://legiscan.com/TX/people/john-doe/id/2",
        },
        2026,
        sos,
    )
    assert senate["slug"] == "2026-tx-sd-3"
    assert senate["is_term_up"] is None
    gov = governor_race("TX", 2026, sos["TX"])
    assert gov["slug"] == "2026-tx-governor"
    assert gov["is_term_up"] is None


class _FakeClient:
    def __init__(self):
        self.configured = True
        self.batches: list[list[dict]] = []

    def submit_elections_jurisdictions(self, records):
        self.batches.append(list(records))
        return {"ok": True, "written": len(records)}


def test_catalog_run_dry_run_does_not_post():
    client = _FakeClient()
    summary = run_catalog(dry_run=True, client=client)
    assert summary["mapped"] == 51
    assert summary["posted"] == 0
    assert client.batches == []
