"""Offline tests for LegiScan bills/votes mapping and coverage notes."""
from ingest.legiscan import member_id_for
from ingest.legiscan_bills import (
    completeness_for_bills,
    in_window,
    map_bill,
    map_member_vote,
    map_vote,
    parse_bill_type,
    run,
)

BILL = {
    "bill_id": 1898133,
    "bill_number": "HB1",
    "bill_type": "B",
    "title": "An Act Relating To A Delaware Department Of Veterans Affairs.",
    "description": "Creates a Department of Veterans Affairs.",
    "status": 4,
    "status_date": "2025-08-13",
    "last_action": "Signed by Governor",
    "url": "https://legiscan.com/DE/bill/HB1/2025",
    "session_id": 2163,
    "session": {"session_id": 2163, "year_start": 2025, "year_end": 2026},
    "history": [{"date": "2025-08-13", "action": "Signed by Governor"}],
    "votes": [
        {
            "roll_call_id": 1574346,
            "date": "2025-05-20",
            "desc": "House Third Reading",
            "yea": 38,
            "nay": 0,
            "nv": 0,
            "absent": 3,
            "passed": 1,
            "chamber": "H",
            "url": "https://legiscan.com/DE/rollcall/HB1/id/1574346",
        }
    ],
}

ROLL = {
    "roll_call_id": 1574346,
    "bill_id": 1898133,
    "date": "2025-05-20",
    "desc": "House Third Reading",
    "yea": 38,
    "nay": 0,
    "nv": 0,
    "absent": 3,
    "passed": 1,
    "chamber": "H",
    "votes": [
        {"people_id": 4411, "vote_id": 1, "vote_text": "Yea"},
        {"people_id": 4428, "vote_id": 1, "vote_text": "Yea"},
        {"people_id": 14549, "vote_id": 4, "vote_text": "Absent"},
        {"vote_id": 2, "vote_text": "Nay"},
    ],
}


def test_parse_bill_type():
    assert parse_bill_type("HB1") == "HB"
    assert parse_bill_type("HJR13") == "HJR"
    assert parse_bill_type("SB200") == "SB"


def test_map_bill_uses_legiscan_url_and_state_fields():
    rec = map_bill(BILL, "DE")
    assert rec["id"] == "LS1898133"
    assert rec["jurisdiction"] == "state"
    assert rec["state_code"] == "DE"
    assert rec["bill_number"] == "HB1"
    assert rec["became_law"] is True
    assert rec["legislation_status"] == "Passed"
    unsigned = dict(BILL, last_action="Enact w/o Sign by Governor", history=[])
    assert map_bill(unsigned, "DE")["became_law"] is True
    assert rec["source_url"] == "https://legiscan.com/DE/bill/HB1/2025"
    assert rec["congress"] == 2025


def test_map_vote_joins_on_people_id_not_name():
    rec = map_vote(ROLL, BILL["votes"][0], BILL, "DE")
    assert rec["source_url"].endswith("/1574346")
    assert rec["rc_id"] == "LSRC1574346"
    assert rec["jurisdiction"] == "state"
    ids = [m["bioguide_id"] for m in rec["member_votes"]]
    assert member_id_for(4411) in ids
    assert "LS14549" in ids
    absent = next(m for m in rec["member_votes"] if m["bioguide_id"] == "LS14549")
    assert absent["vote"] == "not_voting"
    # stub with no people_id is dropped, not name-matched
    assert len(rec["member_votes"]) == 3
    assert rec["_skipped_no_people_id"] == 1


def test_member_vote_normalization():
    assert map_member_vote("Yea") == "yea"
    assert map_member_vote("Nay") == "nay"
    assert map_member_vote("NV") == "not_voting"
    assert map_member_vote("maybe") is None


def test_coverage_notes():
    assert "nothing in the incremental window" in completeness_for_bills(100, 0, 0, 0, None)
    assert "no roll calls" in completeness_for_bills(100, 4, 4, 0, None)
    assert completeness_for_bills(100, 4, 4, 2, None).startswith("full session")


def test_run_offline_fixture():
    from datetime import date

    result = run(
        states=["DE"],
        days_back=9000,
        dry_run=True,
        fixtures={
            "DE": {
                "session": {"session_id": 2163, "year_start": 2025},
                "masterlist": [
                    {
                        "bill_id": 1898133,
                        "number": "HB1",
                        "last_action_date": date.today().isoformat(),
                        "status": 4,
                    }
                ],
                "bills": {"1898133": BILL},
                "roll_calls": {"1574346": ROLL},
            }
        },
    )
    assert result["bills"] == 1
    assert result["votes"] == 1
    assert result["coverage"][0]["state_code"] == "DE"
    assert result["coverage"][0]["last_error"] is None


def test_window_requires_date():
    from datetime import date, timedelta

    since = date.today() - timedelta(days=14)
    assert in_window({"last_action_date": since.isoformat()}, since) is True
    assert in_window({"last_action_date": "1999-01-01"}, since) is False
    assert in_window({}, since) is False


if __name__ == "__main__":
    for fn in [
        test_parse_bill_type,
        test_map_bill_uses_legiscan_url_and_state_fields,
        test_map_vote_joins_on_people_id_not_name,
        test_member_vote_normalization,
        test_coverage_notes,
        test_run_offline_fixture,
        test_window_requires_date,
    ]:
        fn()
        print("ok", fn.__name__)
    print("all passed")
