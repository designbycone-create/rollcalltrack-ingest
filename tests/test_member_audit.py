"""State legislators must not be posted with a congressional bioguide."""
import pytest

from ingest.member_audit import (
    assert_state_member_record,
    classify_members,
    is_state_member_id,
)

WAHAB = {
    "bioguide": "W000832",
    "first": "Aisha",
    "middle": "",
    "nickname": "",
    "last": "Wahab",
    "suffix": "",
    "official_full": "Aisha Wahab",
    "state": "CA",
    "district": 14,
    "chamber": "house",
    "party": "D",
}
BLAIR = {
    "bioguide": "B001328",
    "first": "Everton",
    "middle": "",
    "nickname": "",
    "last": "Blair",
    "suffix": "Jr.",
    "official_full": "Everton Blair Jr.",
    "state": "GA",
    "district": 13,
    "chamber": "house",
    "party": "D",
}
ROSTER = [WAHAB, BLAIR]


def test_state_ids_are_not_congressional_bioguides():
    assert is_state_member_id("LS23231")
    assert not is_state_member_id("W000832")
    assert not is_state_member_id("B001328")
    assert not is_state_member_id("LS")
    assert_state_member_record(
        {
            "jurisdiction": "state",
            "bioguide_id": "LS23231",
            "source_url": "https://legiscan.com/CA/people/23231",
        }
    )
    with pytest.raises(ValueError, match="congressional-style"):
        assert_state_member_record(
            {
                "jurisdiction": "state",
                "bioguide_id": "W000832",
                "source_url": "https://example.test/wahab",
            }
        )
    with pytest.raises(ValueError, match="jurisdiction=state"):
        assert_state_member_record(
            {"jurisdiction": "federal", "bioguide_id": "LS1", "source_url": "https://example.test"}
        )


def test_official_federal_rows_are_kept():
    report = classify_members(
        federal_rows=[
            {
                "id": "W000832",
                "bioguide_id": "W000832",
                "name": "Aisha Wahab",
                "state": "California",
                "chamber": "House",
                "jurisdiction": "federal",
            },
            {
                "id": "B001328",
                "bioguide_id": "B001328",
                "name": "Everton Blair Jr.",
                "state": "Georgia",
                "chamber": "House",
                "jurisdiction": "federal",
            },
        ],
        state_rows=[
            {
                "id": "1dy3szdfo1e0afl",
                "bioguide_id": "LS23231",
                "name": "Aisha Wahab",
                "state_code": "CA",
                "chamber": "senate",
                "district": 10,
                "jurisdiction": "state",
                "source_url": "https://legiscan.com/CA/people/23231",
            },
            {
                "id": "state-other",
                "bioguide_id": "LS2240",
                "name": "Wendell Gilliard",
                "state_code": "SC",
                "chamber": "house",
                "jurisdiction": "state",
                "source_url": "https://legiscan.com/SC/people/2240",
            },
            {
                "id": "bad-state",
                "bioguide_id": "W000832",
                "name": "Aisha Wahab",
                "state_code": "CA",
                "chamber": "house",
                "jurisdiction": "state",
            },
        ],
        official_roster=ROSTER,
    )
    assert report["federal_not_official"] == []
    assert [r["bioguide_id"] for r in report["state_bad_id"]] == ["W000832"]
    overlap_ids = [r["bioguide_id"] for r in report["state_overlap"]]
    assert overlap_ids == ["LS23231", "W000832"]
    wahab = report["state_overlap"][0]
    assert wahab["federal_bioguide"] == "W000832"
    assert wahab["id"] == "1dy3szdfo1e0afl"


def test_unknown_federal_id_is_flagged():
    report = classify_members(
        federal_rows=[
            {
                "id": "LS999",
                "bioguide_id": "LS999",
                "name": "State Person",
                "state": "California",
                "chamber": "House",
                "jurisdiction": "federal",
            }
        ],
        state_rows=[],
        official_roster=ROSTER,
    )
    assert report["federal_not_official"][0]["bioguide_id"] == "LS999"


def test_map_person_still_uses_legiscan_id():
    from ingest.legiscan import map_person

    rec = map_person(
        {
            "people_id": 23231,
            "party": "D",
            "role": "Sen",
            "name": "Aisha Wahab",
            "district": "SD-10",
        },
        "CA",
    )
    assert rec["bioguide_id"] == "LS23231"
    assert rec["jurisdiction"] == "state"
    assert rec["chamber"] == "senate"
    assert_state_member_record(rec)