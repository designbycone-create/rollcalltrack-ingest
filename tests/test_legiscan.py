"""Offline tests for LegiScan legislator mapping."""
from ingest.legiscan import (
    map_person,
    member_id_for,
    normalize_chamber,
    normalize_party,
    parse_district,
    pick_current_session,
    resolve_states,
    run,
    source_url_for,
)

SC_REP = {
    "people_id": 2240,
    "party": "D",
    "role": "Rep",
    "name": "Wendell Gilliard",
    "first_name": "Wendell",
    "last_name": "Gilliard",
    "district": "HD-111",
}

NE_SEN = {
    "people_id": 9999,
    "party": "N",
    "role": "Sen",
    "name": "Jane Nonpartisan",
    "district": "7",
}

NE_R = {
    "people_id": 10001,
    "party": "R",
    "role": "Sen",
    "name": "Pat Independent-label",
    "district": "12",
}


def test_party_matches_federal_dri():
    assert normalize_party("D") == "D"
    assert normalize_party("Democrat") == "D"
    assert normalize_party("Republican") == "R"
    assert normalize_party("I") == "I"
    assert normalize_party("Independent") == "I"
    assert normalize_party("N") == "N"
    assert normalize_party("Nonpartisan") == "N"
    assert normalize_party("Green") is None


def test_nebraska_is_unicameral():
    assert normalize_chamber("NE", "Sen") == "unicameral"
    assert normalize_chamber("NE", "Rep") == "unicameral"
    assert normalize_chamber("SC", "Rep") == "house"
    assert normalize_chamber("TX", "Sen") == "senate"
    assert normalize_chamber("CA", "Assembly") == "house"


def test_district_numeric_from_hd():
    assert parse_district("HD-111") == 111
    assert parse_district("SD-3") == 3
    assert parse_district("12") == 12
    assert parse_district("A") is None
    assert parse_district("") is None


def test_map_sc_rep():
    rec = map_person(SC_REP, "SC")
    assert rec.get("_review") is None
    assert rec["bioguide_id"] == "LS2240"
    assert rec["jurisdiction"] == "state"
    assert rec["state_code"] == "SC"
    assert rec["chamber"] == "house"
    assert rec["party"] == "D"
    assert rec["district"] == 111
    assert rec["source_url"] == source_url_for("SC", 2240)
    assert rec["source_url"].startswith("https://legiscan.com/SC/people/")


def test_committee_sponsor_is_skipped():
    rec = map_person(
        {
            "people_id": 6947,
            "name": "Judiciary Committee",
            "party": "",
            "role": "Sen",
            "district": "",
            "committee_sponsor": 1,
        },
        "NE",
    )
    assert rec["_skip"] is True


def test_nebraska_nonpartisan_is_n_not_independent():
    rec = map_person(NE_SEN, "NE")
    assert rec.get("_review") is None
    assert rec["party"] == "N"
    assert rec["chamber"] == "unicameral"
    rec_r = map_person(NE_R, "NE")
    assert rec_r["chamber"] == "unicameral"
    assert rec_r["party"] == "R"


def test_pick_current_skips_sine_die_and_special():
    sessions = [
        {"session_id": 1, "year_end": 2024, "sine_die": 1, "prior": 1, "special": 0},
        {"session_id": 2, "year_end": 2026, "sine_die": 0, "prior": 0, "special": 1},
        {"session_id": 3, "year_end": 2026, "sine_die": 0, "prior": 0, "special": 0},
    ]
    assert pick_current_session(sessions)["session_id"] == 3


def test_resolve_states():
    assert resolve_states("de", False) == ["DE"]
    assert len(resolve_states(None, True)) == 50
    assert "DC" not in resolve_states(None, True)
    assert "US" not in resolve_states(None, True)


def test_run_offline_roster():
    result = run(
        states=["SC"],
        dry_run=True,
        rosters={"SC": [SC_REP, {"people_id": 1, "party": "X", "role": "Rep", "name": "Odd", "district": "HD-1"}]},
    )
    assert result["mapped"] == 1
    assert result["review"] == 1
    assert result["states"]["SC"]["found"] == 2


def test_member_id_does_not_look_like_bioguide():
    assert member_id_for(2240) == "LS2240"
    assert not member_id_for(2240)[0].isalpha() or member_id_for(2240)[:2] == "LS"


if __name__ == "__main__":
    for fn in [
        test_party_matches_federal_dri,
        test_nebraska_is_unicameral,
        test_district_numeric_from_hd,
        test_map_sc_rep,
        test_committee_sponsor_is_skipped,
        test_nebraska_nonpartisan_is_n_not_independent,
        test_pick_current_skips_sine_die_and_special,
        test_resolve_states,
        test_run_offline_roster,
        test_member_id_does_not_look_like_bioguide,
    ]:
        fn()
        print("ok", fn.__name__)
    print("all passed")
