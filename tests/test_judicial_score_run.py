from datetime import date

from ingest.judicial_score import run


def test_run_dry_run_scores_one_judge():
    events = {
        "13761857": [
            {
                "event_type": "nationwide_injunction",
                "event_date": "2026-01-01",
                "source_url": "https://ex/1",
                "weight": 4,
            }
        ]
    }
    out = run(
        dry_run=True,
        today=date(2026, 8, 15),
        events_by_judge=events,
        snapshot={"scores": {}},
    )
    assert out["mapped"] == 1
    assert out["posted"] == 0
    assert out["dry_run"] is True
    assert out["scores"][0]["judge_id"] == "13761857"
    assert out["scores"][0]["rogue_band"] == "within_bounds"
