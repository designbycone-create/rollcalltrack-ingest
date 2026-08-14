"""Offline tests for judicial nomination linking."""
from ingest.nominations import (
    classify_vote_link,
    current_congress,
    index_votes_by_pn,
    is_judicial,
    map_nomination,
    map_vote_result,
    parse_nominee,
    run,
    senate_vote_id_for,
    source_url_for,
)

ROSTER = [
    {
        "judge_id": "9990001",
        "full_name": "Antonio M. Pozos",
        "court": "U.S. District Court for the Eastern District of Pennsylvania",
        "appointing_president": "Donald J. Trump",
        "source_url": "https://www.fjc.gov/node/9990001",
    },
    {
        "judge_id": "1377101",
        "full_name": "Samuel A. Alito Jr.",
        "court": "Supreme Court of the United States",
        "appointing_president": "George W. Bush",
        "source_url": "https://www.fjc.gov/node/1377101",
    },
    {
        "judge_id": "1001",
        "full_name": "John Smith",
        "court": "U.S. District Court for the District of Maryland",
        "appointing_president": "Barack Obama",
        "source_url": "https://www.fjc.gov/node/1001",
    },
    {
        "judge_id": "1002",
        "full_name": "John Smith",
        "court": "U.S. Court of Appeals for the Fourth Circuit",
        "appointing_president": "Barack Obama",
        "source_url": "https://www.fjc.gov/node/1002",
    },
]

POZOS = {
    "congress": 119,
    "number": 999,
    "citation": "PN999-5",
    "description": (
        "Antonio M. Pozos, of Pennsylvania, to be United States District "
        "Judge for the Eastern District of Pennsylvania, vice Jeffrey L. Schmehl, retired."
    ),
    "organization": "The Judiciary",
    "latestAction": {
        "actionDate": "2026-07-23",
        "text": "Confirmed by the Senate by Yea-Nay Vote. 49 - 44. Record Vote Number: 209.",
    },
}

VOICE = {
    "congress": 118,
    "number": 1460,
    "citation": "PN1460",
    "description": (
        "Jane Q. Public, of Maryland, to be United States District Judge "
        "for the District of Maryland."
    ),
    "latestAction": {
        "actionDate": "2024-09-10",
        "text": "Confirmed by the Senate by Voice Vote.",
    },
}

US_ATTORNEY = {
    "congress": 119,
    "number": 1078,
    "citation": "PN1078",
    "description": "Todd Blanche, of Florida, to be Attorney General.",
    "latestAction": {
        "actionDate": "2026-08-08",
        "text": "Confirmed by the Senate by Yea-Nay Vote. 50 - 49. Record Vote Number: 230.",
    },
}

HZ_VOTES = [
    {
        "id": "s-119-2026-209",
        "result": "Nomination Confirmed",
        "voteType": "On the Nomination PN999-5: Antonio M. Pozos, of Pennsylvania, to be United States District Judge for the Eastern District of Pennsylvania",
        "title": "On the Nomination PN999-5: Antonio M. Pozos",
        "roll": 209,
        "congress": 119,
        "session": "2026",
    },
    {
        "id": "s-119-2026-205",
        "result": "Cloture Motion Agreed to",
        "voteType": "Motion to Invoke Cloture: Antonio M. Pozos to be U.S. District Judge",
        "title": "Motion to Invoke Cloture: Antonio M. Pozos",
        "roll": 205,
    },
]


def test_current_congress_jan3_boundary():
    from datetime import date

    assert current_congress(date(2025, 1, 2)) == 118
    assert current_congress(date(2025, 1, 3)) == 119
    assert current_congress(date(2026, 8, 14)) == 119


def test_judicial_filter_excludes_doj():
    assert is_judicial(POZOS["description"])
    assert not is_judicial(US_ATTORNEY["description"])
    assert not is_judicial("to be Judge Advocate General of the Navy")


def test_parse_nominee_district_and_scotus():
    name, court = parse_nominee(POZOS["description"])
    assert name == "Antonio M. Pozos"
    assert court == "U.S. District Court for the Eastern District of Pennsylvania"
    name, court = parse_nominee(
        "Samuel A. Alito, Jr., of New Jersey, to be an Associate Justice of the Supreme Court of the United States."
    )
    assert "Alito" in (name or "")
    assert court == "Supreme Court of the United States"


def test_vote_result_and_link_classes():
    assert map_vote_result(POZOS["latestAction"]["text"]) == "confirmed"
    assert map_vote_result("Rejected by the Senate by Yea-Nay Vote. 48 - 50.") == "rejected"
    assert map_vote_result("Received in the Senate and referred to the Committee on the Judiciary.") is None
    assert classify_vote_link(POZOS["latestAction"]["text"]) == (209, "roll_call")
    assert classify_vote_link("Confirmed by the Senate by Voice Vote.") == (None, "voice_vote")
    assert classify_vote_link("Confirmed by the Senate by Unanimous Consent.") == (
        None,
        "unanimous_consent",
    )


def test_senate_vote_id_scheme():
    assert senate_vote_id_for(119, "2026-07-23", 209) == "s-119-2026-209"
    assert source_url_for(119, 999).endswith("/119th-congress/999")


def test_index_skips_cloture():
    idx = index_votes_by_pn(HZ_VOTES)
    assert idx["PN999-5"]["id"] == "s-119-2026-209"
    assert idx["PN999-5"]["id"] != "s-119-2026-205"


def test_map_pozos_links_horizons_vote():
    rec = map_nomination(POZOS, [POZOS["latestAction"]], ROSTER, index_votes_by_pn(HZ_VOTES))
    assert rec.get("_review") is None
    assert rec["nomination_id"] == "PN999-5"
    assert rec["judge_id"] == "9990001"
    assert rec["vote_result"] == "confirmed"
    assert rec["vote_date"] == "2026-07-23"
    assert rec["senate_vote_id"] == "s-119-2026-209"
    assert rec["senate_vote_note"] == "linked"
    assert rec["nominating_president"] == "Donald J. Trump"
    assert rec["source_url"].startswith("https://www.congress.gov/nomination/")


def test_voice_vote_null_id_is_labeled():
    rec = map_nomination(VOICE, [VOICE["latestAction"]], ROSTER, {})
    assert rec["_review"] is True  # Jane Q. Public not on roster


def test_voice_vote_known_judge():
    item = dict(VOICE)
    item["description"] = (
        "Antonio M. Pozos, of Pennsylvania, to be United States District "
        "Judge for the Eastern District of Pennsylvania."
    )
    rec = map_nomination(item, [item["latestAction"]], ROSTER, {})
    assert rec["senate_vote_id"] is None
    assert rec["senate_vote_note"] == "voice_vote"
    assert rec["vote_result"] == "confirmed"


def test_attorney_general_is_skipped():
    rec = map_nomination(US_ATTORNEY, [US_ATTORNEY["latestAction"]], ROSTER, {})
    assert rec["_skip"] is True
    assert rec["reason"] == "not_judicial"


def test_ambiguous_name_goes_to_review():
    item = {
        "congress": 118,
        "number": 1,
        "citation": "PN1",
        "description": (
            "John Smith, of Maryland, to be United States District Judge "
            "for the District of Columbia."
        ),
        "latestAction": {
            "actionDate": "2024-01-01",
            "text": "Confirmed by the Senate by Voice Vote.",
        },
    }
    rec = map_nomination(item, [item["latestAction"]], ROSTER, {})
    assert rec["_review"] is True
    assert "ambiguous" in rec["notes"]


def test_run_offline_counts_vote_notes():
    items = [
        POZOS,
        US_ATTORNEY,
        {
            **VOICE,
            "description": (
                "Antonio M. Pozos, of Pennsylvania, to be United States District "
                "Judge for the Eastern District of Pennsylvania."
            ),
        },
    ]
    result = run(
        congress=119,
        dry_run=True,
        roster=ROSTER,
        nominations=items,
        actions_by_number={
            "999": [POZOS["latestAction"]],
            "1078": [US_ATTORNEY["latestAction"]],
            "1460": [VOICE["latestAction"]],
        },
        votes=HZ_VOTES,
    )
    assert result["mapped"] == 2
    assert result["skipped"] == 1
    assert result["vote_notes"]["linked"] == 1
    assert result["vote_notes"]["voice_vote"] == 1


if __name__ == "__main__":
    for fn in [
        test_current_congress_jan3_boundary,
        test_judicial_filter_excludes_doj,
        test_parse_nominee_district_and_scotus,
        test_vote_result_and_link_classes,
        test_senate_vote_id_scheme,
        test_index_skips_cloture,
        test_map_pozos_links_horizons_vote,
        test_voice_vote_null_id_is_labeled,
        test_voice_vote_known_judge,
        test_attorney_general_is_skipped,
        test_ambiguous_name_goes_to_review,
        test_run_offline_counts_vote_notes,
    ]:
        fn()
        print("ok", fn.__name__)
    print("all passed")
