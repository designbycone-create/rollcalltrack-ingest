from ingest.courtlistener_events import map_cluster, match_judge

ROSTER = [
    {
        "judge_id": "13761857",
        "full_name": "Adam Ben Abelson",
        "court": "U.S. District Court for the District of Maryland",
    },
    {
        "judge_id": "1393931",
        "full_name": "Ronnie Abrams",
        "court": "U.S. District Court for the Southern District of New York",
    },
]


def test_match_unique_name():
    assert match_judge("Adam Ben Abelson", "District of Maryland", ROSTER) == "13761857"


def test_match_ambiguous_or_unknown_is_none():
    assert match_judge("John Smith", "District of Maryland", ROSTER) is None


def test_map_nationwide_writes_event():
    cluster = {
        "absolute_url": "/opinion/99/md-case/",
        "date_filed": "2026-02-01",
        "case_name": "Smith v. United States",
        "court": "District of Maryland",
        "judges": "Adam Ben Abelson",
        "plain_text": "The court grants a nationwide injunction against the agency.",
    }
    out = map_cluster(cluster, ROSTER)
    assert out["status"] == "write"
    assert out["event"]["judge_id"] == "13761857"
    assert out["event"]["event_type"] == "nationwide_injunction"
    assert out["event"]["source_url"].startswith("https://www.courtlistener.com/")
    assert out["event"]["weight"] == 4


def test_map_bare_injunction_is_review():
    cluster = {
        "absolute_url": "/opinion/100/md-case/",
        "date_filed": "2026-02-01",
        "case_name": "Doe v. United States",
        "court": "District of Maryland",
        "judges": "Adam Ben Abelson",
        "plain_text": "A preliminary injunction is granted as to the named plaintiffs only.",
    }
    out = map_cluster(cluster, ROSTER)
    assert out["status"] == "review"
    assert "event" not in out or out.get("event") is None
