"""Per-run member lookup cache, and 429 exhaustion is not a miss."""
import logging

import pytest
import requests

from ingest.ingest_client import (
    LOOKUP_ATTEMPTS,
    HorizonsIngestClient,
    LookupRateLimited,
)


class _Resp:
    def __init__(self, status, data=None, headers=None, text=None):
        self.status_code = status
        self._data = data if data is not None else {}
        self.headers = headers or {}
        self.text = text if text is not None else str(self._data)
        self.url = "https://example.test/hcgi"

    def json(self):
        if isinstance(self._data, ValueError):
            raise self._data
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


def _client():
    return HorizonsIngestClient("https://example.test", "not-a-real-key")


def _member(name="John Boozman", bioguide="B001236"):
    return {
        "found": True,
        "member": {
            "id": bioguide,
            "bioguide_id": bioguide,
            "name": name,
            "state": "Arkansas",
        },
    }


def test_lookup_caches_hit_and_miss_without_pacing_the_repeat(monkeypatch):
    hz = _client()
    calls = []
    sleeps = []
    monkeypatch.setattr("ingest.ingest_client.time.sleep", lambda seconds: sleeps.append(seconds))

    def fake_get(path, params=None):
        calls.append(dict(params or {}))
        if params and params.get("name") == "Missing Person":
            return _Resp(200, {"found": False})
        return _Resp(200, _member())

    monkeypatch.setattr(hz, "_get", fake_get)
    first = hz._lookup_http(name="John  Boozman", state="Arkansas", chamber="senate")
    first["name"] = "mutated"
    second = hz._lookup_http(name="john boozman", state="OH")
    # OH is a different state, so this is not the cached Arkansas row.
    assert second["name"] == "John Boozman"
    third = hz._lookup_http(name="John Boozman", state="oh")
    assert third["name"] == "John Boozman"
    ohio = hz._lookup_http(name="John Boozman", state="Ohio", chamber="house")
    assert ohio["name"] == "John Boozman"
    again = hz._lookup_http(name="John Boozman", state="Arkansas", chamber="senate")
    assert again["name"] == "John Boozman"
    assert again is not first
    miss = hz._lookup_http(name="Missing Person", state="Arkansas", chamber="senate")
    assert miss is None
    assert hz._lookup_http(name="Missing Person", state="Arkansas", chamber="senate") is None

    assert len(calls) == 4
    assert all("chamber" not in call for call in calls)
    # First origin call does not wait. Each later origin call does.
    # Cache hits do not call the origin and do not sleep.
    assert len(sleeps) == 3


def test_usps_and_full_state_share_one_cache_entry(monkeypatch):
    hz = _client()
    calls = []
    monkeypatch.setattr("ingest.ingest_client.time.sleep", lambda _seconds: None)

    def fake_get(path, params=None):
        calls.append(dict(params or {}))
        return _Resp(200, _member())

    monkeypatch.setattr(hz, "_get", fake_get)
    assert hz._lookup_http(name="John Boozman", state="AR")["id"] == "B001236"
    assert hz._lookup_http(name="John Boozman", state="Arkansas")["id"] == "B001236"
    assert len(calls) == 1
    assert calls[0]["state"] == "AR"


def test_client_errors_are_cached_and_server_errors_are_not(monkeypatch):
    hz = _client()
    monkeypatch.setattr("ingest.ingest_client.time.sleep", lambda _seconds: None)
    statuses = iter([404, 503, 503])
    calls = {"n": 0}

    def fake_get(path, params=None):
        calls["n"] += 1
        return _Resp(next(statuses), {"error": "no"})

    monkeypatch.setattr(hz, "_get", fake_get)
    assert hz._lookup_http(name="John Boozman", state="Arkansas") is None
    assert hz._lookup_http(name="John Boozman", state="Arkansas") is None
    assert calls["n"] == 1

    hz2 = _client()
    calls["n"] = 0
    monkeypatch.setattr(hz2, "_get", fake_get)
    assert hz2._lookup_http(name="Tom Cotton", state="Arkansas") is None
    assert hz2._lookup_http(name="Tom Cotton", state="Arkansas") is None
    assert calls["n"] == 2


def test_429_exhaustion_raises_and_is_not_cached(monkeypatch, caplog):
    hz = _client()
    monkeypatch.setattr("ingest.ingest_client.time.sleep", lambda _seconds: None)
    calls = {"n": 0}

    def fake_get(path, params=None):
        calls["n"] += 1
        return _Resp(429, text='{"error":"Too many requests, please try again later"}')

    monkeypatch.setattr(hz, "_get", fake_get)
    with caplog.at_level(logging.INFO):
        with pytest.raises(LookupRateLimited):
            hz._lookup_http(name="John Boozman", state="Arkansas")
        with pytest.raises(LookupRateLimited):
            hz._lookup_http(name="John Boozman", state="Arkansas")
    assert calls["n"] == LOOKUP_ATTEMPTS * 2
    assert "MISS" not in caplog.text


def test_rate_limited_name_still_looks_up_known_bioguide(monkeypatch, caplog):
    hz = _client()
    calls = []

    def fake_lookup(name=None, state=None, chamber=None, bioguide_id=None):
        calls.append(
            {
                "name": name,
                "state": state,
                "chamber": chamber,
                "bioguide_id": bioguide_id,
            }
        )
        if bioguide_id == "B001236":
            return _member()["member"]
        raise LookupRateLimited("lookup HTTP 429")

    monkeypatch.setattr(hz, "_lookup_http", fake_lookup)
    monkeypatch.setattr(
        "ingest.ingest_client.match_legislator",
        lambda *args, **kwargs: {
            "bioguide": "B001236",
            "first": "John",
            "last": "Boozman",
            "state": "AR",
            "official_full": "John Boozman",
            "party": "Republican",
        },
    )
    with caplog.at_level(logging.INFO):
        member = hz.lookup_member_by_name(
            "John Boozman",
            office="Boozman, John (Senator)",
            chamber="senate",
        )
    assert member["bioguide_id"] == "B001236"
    assert sum(1 for call in calls if call["name"]) == 1
    assert calls[-1]["bioguide_id"] == "B001236"
    assert "Member lookup MISS" not in caplog.text
    assert "not a miss" in caplog.text


def test_rate_limited_name_and_bioguide_raise(monkeypatch, caplog):
    hz = _client()
    calls = []

    def fake_lookup(**params):
        calls.append(params)
        raise LookupRateLimited("lookup HTTP 429")

    monkeypatch.setattr(hz, "_lookup_http", fake_lookup)
    monkeypatch.setattr(
        "ingest.ingest_client.match_legislator",
        lambda *args, **kwargs: {
            "bioguide": "B001236",
            "state": "AR",
            "official_full": "John Boozman",
            "first": "John",
            "last": "Boozman",
        },
    )
    with caplog.at_level(logging.INFO):
        with pytest.raises(LookupRateLimited):
            hz.lookup_member_by_name(
                "John Boozman",
                office="Boozman, John (Senator)",
                bioguide_id="B001236",
                chamber="senate",
            )
    assert sum(1 for call in calls if call.get("name")) == 1
    assert calls[-1]["bioguide_id"] == "B001236"
    assert "Member lookup MISS" not in caplog.text


def test_lookup_uses_office_state_and_roster_bioguide(monkeypatch):
    hz = _client()
    calls = []
    matched = {}

    def fake_match(name, **kwargs):
        matched["state"] = kwargs.get("state")
        return {
            "bioguide": "M000355",
            "state": "KY",
            "official_full": "Mitch McConnell",
            "first": "Mitch",
            "last": "McConnell",
            "party": "Republican",
        }

    def fake_lookup(**params):
        calls.append(params)
        if params.get("bioguide_id") == "M000355":
            return {
                "id": "mem-mcconnell",
                "bioguide_id": "M000355",
                "name": "Mitch McConnell",
                "state": "Kentucky",
            }
        return None

    monkeypatch.setattr("ingest.ingest_client.match_legislator", fake_match)
    monkeypatch.setattr(hz, "_lookup_http", fake_lookup)
    member = hz.lookup_member_by_name(
        "A. Mitchell McConnell, Jr.",
        office="Senator (KY)",
        chamber="senate",
    )
    assert matched["state"] == "KY"
    assert calls[0]["state"] == "Kentucky"
    assert calls[0]["chamber"] == "senate"
    assert any(call.get("bioguide_id") == "M000355" for call in calls)
    assert member["id"] == "mem-mcconnell"
    assert member["party"] == "Republican"


def test_ingestion_log_retries_once_on_500_and_logs_the_body(monkeypatch, caplog):
    hz = _client()
    calls = {"n": 0}

    def fake_post(body, path="/hcgi/api/ingest/disclosures"):
        calls["n"] += 1
        assert body["payload"]["source"] == "senate_votes"
        if calls["n"] == 1:
            return _Resp(500, text='{"error":"Failed to create record."}')
        return _Resp(200, {"ok": True})

    monkeypatch.setattr(hz, "_post_ingest", fake_post)
    with caplog.at_level(logging.WARNING):
        result = hz.submit_ingestion_log("senate_votes", 1, 1, 0, note="menu")
    assert result == {"ok": True}
    assert calls["n"] == 2
    assert "Failed to create record." in caplog.text
    assert "response body" in caplog.text
    assert "retrying once" in caplog.text


def test_ingestion_log_does_not_retry_a_client_error(monkeypatch, caplog):
    hz = _client()
    calls = {"n": 0}

    def fake_post(body, path="/hcgi/api/ingest/disclosures"):
        calls["n"] += 1
        return _Resp(400, text="bad request")

    monkeypatch.setattr(hz, "_post_ingest", fake_post)
    with caplog.at_level(logging.WARNING):
        result = hz.submit_ingestion_log("senate_votes", 1, 0, 1)
    assert result is None
    assert calls["n"] == 1
    assert "bad request" in caplog.text
    assert "retrying once" not in caplog.text
