"""Retry policy for Horizons lookup/ingest and the backoff helper."""
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from http.client import RemoteDisconnected

import requests

from ingest.http_retry import backoff_seconds, is_connection_error
from ingest.ingest_client import LOOKUP_MIN_INTERVAL, HorizonsIngestClient


class _Resp:
    def __init__(self, status, data=None, headers=None, text=None):
        self.status_code = status
        self._data = data if data is not None else {}
        self.headers = headers or {}
        self.text = text if text is not None else str(self._data)
        self.url = "https://example.test/hcgi"

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code} for {self.url}", response=self)


def _disconnect(message="Remote end closed connection without response"):
    return requests.exceptions.ConnectionError(
        "Connection aborted.",
        RemoteDisconnected(message),
    )


def test_backoff_is_exponential_with_jitter_and_honors_retry_after(monkeypatch):
    monkeypatch.setattr("ingest.http_retry.random.random", lambda: 0.0)
    # 50% of base * 2**attempt when random() is 0.
    assert backoff_seconds(0, base=2.0, cap=60.0) == 1.0
    assert backoff_seconds(1, base=2.0, cap=60.0) == 2.0
    assert backoff_seconds(3, base=2.0, cap=60.0) == 8.0
    monkeypatch.setattr("ingest.http_retry.random.random", lambda: 1.0)
    assert backoff_seconds(2, base=2.0, cap=60.0) == 8.0
    # Cap applies before jitter.
    assert backoff_seconds(8, base=2.0, cap=10.0) == 10.0

    early = _Resp(429, headers={"Retry-After": "7"})
    monkeypatch.setattr("ingest.http_retry.random.random", lambda: 0.0)
    assert backoff_seconds(0, early, base=1.0, cap=60.0) == 7.0

    when = datetime.now(timezone.utc) + timedelta(seconds=30)
    dated = _Resp(429, headers={"Retry-After": format_datetime(when)})
    delay = backoff_seconds(0, dated, base=1.0, cap=60.0)
    assert delay >= 20


def test_connection_error_includes_remote_disconnected():
    assert is_connection_error(_disconnect())
    assert is_connection_error(RemoteDisconnected("Remote end closed connection without response"))
    assert is_connection_error(requests.exceptions.ConnectionError("reset"))
    assert not is_connection_error(requests.exceptions.InvalidURL("nope"))


def test_boozman_arkansas_survives_429_then_disconnect(monkeypatch):
    """Run 39795750445 missed John Boozman after lookup 429s and a disconnect.

    The same run had already HIT ``state='Arkansas'``. congress-legislators
    stores state ``AR``; ``/members/lookup`` needs the full name. The miss
    was rate limiting and RemoteDisconnected, not the state field.
    """
    hz = HorizonsIngestClient("https://example.test", "not-a-real-key")
    calls = []

    def fake_get(path, params=None):
        calls.append(dict(params or {}))
        if len(calls) == 1:
            return _Resp(429, {"error": "Too many requests"}, headers={"Retry-After": "1"})
        if len(calls) == 2:
            raise _disconnect()
        return _Resp(
            200,
            {
                "found": True,
                "member": {
                    "id": "B001236",
                    "bioguide_id": "B001236",
                    "name": "John Boozman",
                    "state": "Arkansas",
                },
            },
        )

    monkeypatch.setattr(hz, "_get", fake_get)
    monkeypatch.setattr("ingest.ingest_client.time.sleep", lambda _seconds: None)
    monkeypatch.setattr(
        "ingest.ingest_client.match_legislator",
        lambda *args, **kwargs: {
            "bioguide": "B001236",
            "state": "AR",
            "first": "John",
            "last": "Boozman",
            "official_full": "John Boozman",
            "party": "R",
            "chamber": "senate",
        },
    )
    member = hz.lookup_member_by_name(
        "John Boozman",
        office="Boozman, John (Senator)",
        chamber="senate",
    )
    assert member["bioguide_id"] == "B001236"
    assert calls[0] == {"name": "John Boozman", "state": "Arkansas"}
    assert len(calls) == 3


def test_lookup_retries_429_honoring_retry_after(monkeypatch):
    hz = HorizonsIngestClient("https://example.test", "not-a-real-key")
    sleeps = []
    monkeypatch.setattr("ingest.ingest_client.time.sleep", lambda seconds: sleeps.append(seconds))
    calls = {"n": 0}

    def fake_get(path, params=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return _Resp(429, {"error": "Too many requests"}, headers={"Retry-After": "7"})
        return _Resp(
            200,
            {
                "found": True,
                "member": {
                    "id": "B001236",
                    "bioguide_id": "B001236",
                    "name": "John Boozman",
                    "state": "Arkansas",
                },
            },
        )

    monkeypatch.setattr(hz, "_get", fake_get)
    member = hz._lookup_http(name="John Boozman", state="Arkansas")
    assert member["bioguide_id"] == "B001236"
    assert calls["n"] == 2
    assert sleeps[0] >= 7


def test_lookup_retries_remote_disconnected_then_hits(monkeypatch):
    hz = HorizonsIngestClient("https://example.test", "not-a-real-key")
    monkeypatch.setattr("ingest.ingest_client.time.sleep", lambda _seconds: None)
    calls = {"n": 0}

    def fake_get(path, params=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _disconnect()
        return _Resp(
            200,
            {
                "found": True,
                "member": {
                    "id": "B001236",
                    "bioguide_id": "B001236",
                    "name": "John Boozman",
                    "state": "Arkansas",
                },
            },
        )

    monkeypatch.setattr(hz, "_get", fake_get)
    member = hz._lookup_http(name="John Boozman", state="Arkansas")
    assert calls["n"] == 2
    assert member["name"] == "John Boozman"


def test_lookup_throttles_between_calls(monkeypatch):
    hz = HorizonsIngestClient("https://example.test", "not-a-real-key")
    clock = {"t": 5000.0}
    sleeps = []

    def monotonic():
        return clock["t"]

    def sleep(seconds):
        sleeps.append(seconds)
        clock["t"] += seconds

    monkeypatch.setattr("ingest.ingest_client.time.monotonic", monotonic)
    monkeypatch.setattr("ingest.ingest_client.time.sleep", sleep)

    def fake_get(path, params=None):
        return _Resp(
            200,
            {"found": True, "member": {"id": "B001236", "bioguide_id": "B001236", "name": "John Boozman"}},
        )

    monkeypatch.setattr(hz, "_get", fake_get)
    assert hz._lookup_http(name="John Boozman", state="Arkansas")["id"] == "B001236"
    assert hz._lookup_http(name="John Boozman", state="Arkansas")["id"] == "B001236"
    assert sleeps == [LOOKUP_MIN_INTERVAL]


def test_ingest_submit_retries_disconnect_then_succeeds(monkeypatch):
    hz = HorizonsIngestClient("https://example.test", "not-a-real-key")
    calls = {"n": 0}

    def fake_post(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _disconnect()
        return _Resp(200, {"filing_id": "ok", "transactions_written": 1})

    monkeypatch.setattr(hz.session, "post", fake_post)
    monkeypatch.setattr("ingest.ingest_client.time.sleep", lambda _seconds: None)
    result = hz.submit_filing(
        {
            "source_url": "https://efdsearch.senate.gov/search/view/ptr/f727289a-4d72-4c90-a3ea-63d5bcd23398/",
            "member_name": "John Boozman",
            "filing_date": "10/22/2026",
        },
        transactions=[{"asset_name": "Example Fund", "transaction_type": "purchase"}],
    )
    assert calls["n"] == 2
    assert result["filing_id"] == "ok"


def test_ingest_submit_still_fails_when_retries_are_exhausted(monkeypatch):
    hz = HorizonsIngestClient("https://example.test", "not-a-real-key")

    def fake_post(*_args, **_kwargs):
        raise _disconnect()

    monkeypatch.setattr(hz.session, "post", fake_post)
    monkeypatch.setattr("ingest.ingest_client.time.sleep", lambda _seconds: None)
    try:
        hz.submit_filing(
            {
                "source_url": "https://efdsearch.senate.gov/search/view/ptr/exhausted/",
                "member_name": "John Boozman",
                "filing_date": "10/22/2026",
            },
            transactions=[],
        )
    except requests.ConnectionError as exc:
        assert is_connection_error(exc)
    else:
        raise AssertionError("exhausted connection errors must still fail the submit")
