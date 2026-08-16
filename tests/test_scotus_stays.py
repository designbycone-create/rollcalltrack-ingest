from ingest.scotus_stays import map_scotus_order

ROSTER = [
    {
        "judge_id": "1393931",
        "full_name": "Ronnie Abrams",
        "court": "U.S. District Court for the Southern District of New York",
    }
]


def test_stay_writes_scotus_stay():
    out = map_scotus_order(
        {
            "source_url": "https://www.supremecourt.gov/orders/2026/stay1.pdf",
            "date_filed": "2026-03-01",
            "case_name": "United States v. Example",
            "lower_court": "S.D.N.Y.",
            "lower_judge": "Ronnie Abrams",
            "disposition": "stayed",
        },
        ROSTER,
    )
    assert out["status"] == "write"
    assert out["event"]["event_type"] == "scotus_stay"
    assert out["event"]["classification"] == "higher_court_disposition"
    assert out["event"]["weight"] == 5
    assert out["event"]["judge_id"] == "1393931"


def test_unmatched_lower_judge_is_review():
    out = map_scotus_order(
        {
            "source_url": "https://www.supremecourt.gov/orders/2026/stay2.pdf",
            "date_filed": "2026-03-01",
            "case_name": "United States v. Example",
            "lower_court": "S.D.N.Y.",
            "lower_judge": "Nobody Known",
            "disposition": "reversed",
        },
        ROSTER,
    )
    assert out["status"] == "review"
