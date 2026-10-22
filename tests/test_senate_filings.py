"""Senate eFD fetch retries and the run summary for persistent failures."""
from datetime import date
from http.client import RemoteDisconnected

import pytest
import requests

from ingest.ingest_client import HorizonsIngestClient
from ingest.senate import parse_filing_detail, run, search_recent_filings


PTR_URL = "https://efdsearch.senate.gov/search/view/ptr/f727289a-4d72-4c90-a3ea-63d5bcd23398/"
PAPER_URL = "https://efdsearch.senate.gov/search/view/paper/ec20cd93-6702-4a29-b3a6-983f4b17f365/"


class _Resp:
    def __init__(self, status, body="", url=""):
        self.status_code = status
        self.text = body if isinstance(body, str) else ""
        self._body = body
        self.url = url
        self.headers = {}

    def json(self):
        if isinstance(self._body, dict):
            return self._body
        raise ValueError("not json")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(
                f"HTTP {self.status_code} for url: {self.url}",
                response=self,
            )


def _disconnect():
    return requests.exceptions.ConnectionError(
        "Connection aborted.",
        RemoteDisconnected("Remote end closed connection without response"),
    )


def test_parse_filing_detail_retries_5xx_and_connection_abort(monkeypatch):
    monkeypatch.setattr("ingest.senate.time.sleep", lambda _seconds: None)

    class Session:
        def __init__(self):
            self.n = 0

        def get(self, url, timeout=None):
            self.n += 1
            if self.n == 1:
                return _Resp(503, "unavailable", url)
            if self.n == 2:
                raise _disconnect()
            return _Resp(200, "<html><body>no table</body></html>", url)

    detail = parse_filing_detail(Session(), PTR_URL)
    assert detail["transactions"] == []
    assert detail["paper"] is False


def test_search_retries_connection_abort_then_returns_rows(monkeypatch):
    monkeypatch.setattr("ingest.senate.time.sleep", lambda _seconds: None)

    class Session:
        def __init__(self):
            self.n = 0

        def post(self, url, data=None, timeout=None, headers=None):
            self.n += 1
            if self.n == 1:
                raise _disconnect()
            return _Resp(
                200,
                {"data": [], "recordsFiltered": 0},
                url,
            )

    rows = search_recent_filings(Session(), date(2026, 7, 24), csrf_token="csrf")
    assert rows == []


def _filing(url=PTR_URL, name="John Boozman"):
    return {
        "name": name,
        "office": "Boozman, John (Senator)",
        "filing_type": "Periodic Transaction Report",
        "filing_date": "10/22/2026",
        "detail_url": url,
    }


def _client():
    return HorizonsIngestClient("https://example.test", "not-a-real-key")


def test_senate_run_keeps_going_when_fetch_recovers(monkeypatch):
    monkeypatch.setattr("ingest.senate.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("ingest.senate._accept_agreement", lambda session: "csrf")
    monkeypatch.setattr(
        "ingest.senate.search_recent_filings",
        lambda *args, **kwargs: [_filing()],
    )

    class Session:
        def __init__(self):
            self.n = 0

        def get(self, url, timeout=None):
            self.n += 1
            if self.n == 1:
                raise _disconnect()
            return _Resp(200, "<html></html>", url)

    monkeypatch.setattr("ingest.senate._new_session", lambda: Session())
    client = _client()
    monkeypatch.setattr(
        client,
        "lookup_member_by_name",
        lambda *args, **kwargs: {
            "id": "B001236",
            "bioguide_id": "B001236",
            "name": "John Boozman",
            "state": "Arkansas",
            "party": "R",
        },
    )
    monkeypatch.setattr(
        client,
        "submit_filing",
        lambda *args, **kwargs: {"filing_id": "ok", "transactions_written": 1},
    )
    monkeypatch.setattr(client, "submit_ingestion_log", lambda *args, **kwargs: None)

    result = run(days_back=90, client=client, max_filings=1)
    assert result["failed"] == 0
    assert result["parsed"] == 1
    assert result["failures"] == []


def test_senate_run_records_url_and_reason_when_submit_retries_exhausted(monkeypatch):
    monkeypatch.setattr("ingest.senate.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("ingest.ingest_client.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("ingest.senate._accept_agreement", lambda session: "csrf")
    monkeypatch.setattr(
        "ingest.senate.search_recent_filings",
        lambda *args, **kwargs: [_filing(PAPER_URL, "RICHARD  BLUMENTHAL")],
    )
    monkeypatch.setattr(
        "ingest.senate.parse_filing_detail",
        lambda session, url: {"transactions": [], "raw_html": "", "paper": True},
    )
    monkeypatch.setattr("ingest.senate._new_session", lambda: object())

    client = _client()
    monkeypatch.setattr(client, "lookup_member_by_name", lambda *args, **kwargs: None)

    def explode(*_args, **_kwargs):
        raise _disconnect()

    monkeypatch.setattr(client.session, "post", explode)
    monkeypatch.setattr(client, "submit_ingestion_log", lambda *args, **kwargs: None)

    result = run(days_back=90, client=client, max_filings=1)
    assert result["parsed"] == 0
    assert result["failed"] == 1
    assert result["failures"][0]["source_url"] == PAPER_URL
    reason = result["failures"][0]["reason"]
    assert "RemoteDisconnected" in reason or "Connection aborted" in reason


def test_successful_retry_does_not_fail_the_job(monkeypatch):
    """A 429 or disconnect that succeeds on retry is not a failed record."""
    monkeypatch.setattr("ingest.senate.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("ingest.ingest_client.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("ingest.senate._accept_agreement", lambda session: "csrf")
    monkeypatch.setattr(
        "ingest.senate.search_recent_filings",
        lambda *args, **kwargs: [_filing()],
    )
    monkeypatch.setattr(
        "ingest.senate.parse_filing_detail",
        lambda session, url: {"transactions": [], "raw_html": "", "paper": False},
    )
    monkeypatch.setattr("ingest.senate._new_session", lambda: object())

    client = _client()
    monkeypatch.setattr(
        client,
        "lookup_member_by_name",
        lambda *args, **kwargs: {
            "id": "B001236",
            "bioguide_id": "B001236",
            "name": "John Boozman",
            "state": "Arkansas",
        },
    )
    calls = {"n": 0}

    def fake_post(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _Resp(429, {"error": "Too many requests"}, PTR_URL)
        return _Resp(200, {"filing_id": "recovered", "transactions_written": 0}, PTR_URL)

    # _Resp used by session.post needs no raise until submit_filing asks.
    monkeypatch.setattr(client.session, "post", fake_post)
    monkeypatch.setattr(client, "submit_ingestion_log", lambda *args, **kwargs: None)

    result = run(days_back=90, client=client, max_filings=1)
    assert calls["n"] >= 2
    assert result["failed"] == 0
    assert result["parsed"] == 1
    assert result["failures"] == []


def test_main_exits_1_and_names_the_source_url(monkeypatch, caplog):
    import logging
    import sys

    import ingest.main as mainmod

    url = PTR_URL
    monkeypatch.setattr(
        mainmod.senate,
        "run",
        lambda *args, **kwargs: {
            "found": 1,
            "parsed": 0,
            "failed": 1,
            "failures": [
                {
                    "source_url": url,
                    "reason": "('Connection aborted.', RemoteDisconnected('Remote end closed connection without response'))",
                }
            ],
        },
    )
    monkeypatch.setattr(sys, "argv", ["ingest.main", "--source", "senate", "--dry-run"])
    with caplog.at_level(logging.ERROR):
        with pytest.raises(SystemExit) as exc:
            mainmod.main()
    assert exc.value.code == 1
    assert url in caplog.text
    assert "RemoteDisconnected" in caplog.text


def test_main_does_not_exit_when_retries_succeeded(monkeypatch):
    import sys

    import ingest.main as mainmod

    monkeypatch.setattr(
        mainmod.senate,
        "run",
        lambda *args, **kwargs: {"found": 1, "parsed": 1, "failed": 0, "failures": []},
    )
    monkeypatch.setattr(sys, "argv", ["ingest.main", "--source", "senate", "--dry-run"])
    mainmod.main()
