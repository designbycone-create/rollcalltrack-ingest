from datetime import date

from ingest.judicial_score import recency_multiplier, score_judge

TODAY = date(2026, 8, 15)


def _event(event_type, days_ago, url="https://example.com/a"):
    from datetime import timedelta

    return {
        "event_type": event_type,
        "event_date": (TODAY - timedelta(days=days_ago)).isoformat(),
        "source_url": url,
        "weight": None,
    }


def test_three_event_golden_raw_and_score():
    events = [
        _event("nationwide_injunction", 30, "https://ex/1"),
        _event("nationwide_injunction", 40, "https://ex/2"),
        _event("scotus_stay", 10, "https://ex/3"),
    ]
    out = score_judge(events, today=TODAY)
    # raw = 4+4+5 = 13; score = round(13/20*100) = 65
    assert out["raw"] == 13.0
    assert out["rogue_score"] == 65
    assert out["rogue_band"] == "rogue"
    assert out["event_count"] == 3


def test_zero_events_is_unscored():
    out = score_judge([], today=TODAY)
    assert out["rogue_score"] == 0
    assert out["rogue_band"] == "unscored"
    assert out["event_count"] == 0


def test_sixteen_year_old_stay_contributes_zero():
    events = [_event("scotus_stay", 16 * 365 + 10)]
    out = score_judge(events, today=TODAY)
    assert out["raw"] == 0.0
    assert out["rogue_band"] == "unscored"
    assert out["event_count"] == 0


def test_recency_buckets():
    assert recency_multiplier(date(2024, 8, 15), TODAY) == 1.0
    assert recency_multiplier(date(2019, 8, 15), TODAY) == 0.6
    assert recency_multiplier(date(2014, 8, 15), TODAY) == 0.3
    assert recency_multiplier(date(2010, 8, 15), TODAY) == 0.0


def test_ideology_not_inferred_from_party():
    out = score_judge([], today=TODAY, ideology_score=None, ideology_prior_only=True)
    assert out["ideology_score"] is None
    assert out["ideology_label"] == "prior_only"


def test_ideology_labels():
    lib = score_judge([], today=TODAY, ideology_score=-40)
    mid = score_judge([], today=TODAY, ideology_score=0)
    con = score_judge([], today=TODAY, ideology_score=40)
    assert lib["ideology_label"] == "liberal"
    assert mid["ideology_label"] == "moderate"
    assert con["ideology_label"] == "conservative"
