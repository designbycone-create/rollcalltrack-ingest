import pytest

from ingest.ingest_client import HorizonsIngestClient


class FakeResp:
    def __init__(self, status=200, data=None):
        self.status_code = status
        self._data = data or {"ok": True, "written": 1}
        self.text = ""

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


def test_submit_events_requires_source_url_and_judge_id():
    hz = HorizonsIngestClient("https://example.com", "k")
    with pytest.raises(ValueError):
        hz.submit_judicial_events([{"event_id": "x", "event_type": "scotus_stay"}])


def test_submit_events_posts_batch(monkeypatch):
    hz = HorizonsIngestClient("https://example.com", "k")
    seen = {}

    def fake_post(body, path="/hcgi/api/ingest/disclosures"):
        seen["body"] = body
        seen["path"] = path
        return FakeResp(200, {"ok": True, "written": 1})

    monkeypatch.setattr(hz, "_post_ingest", fake_post)
    rec = {
        "event_id": "abc",
        "judge_id": "13761857",
        "event_type": "nationwide_injunction",
        "event_date": "2026-01-01",
        "court": "S.D.N.Y.",
        "source_url": "https://www.courtlistener.com/opinion/1/",
        "classification": "explicit_text",
        "weight": 4,
    }
    out = hz.submit_judicial_events([rec])
    assert seen["path"] == "/hcgi/api/ingest/judicial/events"
    assert seen["body"]["events"][0]["judge_id"] == "13761857"
    assert out["written"] == 1


def test_submit_scores_posts_batch(monkeypatch):
    hz = HorizonsIngestClient("https://example.com", "k")
    seen = {}

    def fake_post(body, path="/hcgi/api/ingest/disclosures"):
        seen["path"] = path
        seen["body"] = body
        return FakeResp(200, {"ok": True, "written": 1})

    monkeypatch.setattr(hz, "_post_ingest", fake_post)
    rec = {
        "judge_id": "13761857",
        "rogue_score": 65,
        "rogue_band": "rogue",
        "event_count": 3,
        "scored_through": "2026-08-15",
    }
    hz.submit_judicial_scores([rec])
    assert seen["path"] == "/hcgi/api/ingest/judicial/scores"
    assert seen["body"]["scores"][0]["rogue_band"] == "rogue"
