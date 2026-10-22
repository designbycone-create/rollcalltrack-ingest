"""Filings and transactions that look like fixtures are not posted."""
import logging
from datetime import date, timedelta

import pytest

from ingest import db
from ingest.ingest_client import HorizonsIngestClient
from tests.test_house_ptr import _MemoryPB, _Resp


def _filing(**overrides):
    today = date.today().isoformat()
    body = {
        "source_url": "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/20035588.pdf",
        "member_name": "Kevin Hern",
        "member_id": "hern-id",
        "member_party": "R",
        "chamber": "house",
        "filing_type": "ptr",
        "filing_date": today,
    }
    body.update(overrides)
    return body


def _txn(**overrides):
    body = {
        "filing_id": "filing-1",
        "line_index": 0,
        "asset_name": "Lockheed Martin Corporation Common Stock (LMT)",
        "transaction_date": "09/17/2026",
        "amount_range_raw": "$15,001 - $50,000",
        "ticker": "LMT",
        "ticker_confidence": "medium",
        "member_id": "hern-id",
    }
    body.update(overrides)
    return body


def test_skips_fixture_filings_and_transactions(caplog):
    conn = _MemoryPB()
    far_future = (date.today() + timedelta(days=366)).isoformat()
    far_past = (date.today() - timedelta(days=366)).isoformat()
    rejected = [
        _filing(source_url="https://example.com/ptr.pdf"),
        _filing(source_url="https://cdn.example.org/ptr.pdf"),
        _filing(member_name="  ", filer_name=""),
        _filing(member_id="Z9FAKE"),
        _filing(member_id="test-member"),
        _filing(filing_date=far_future),
        _filing(filing_date=far_past),
    ]
    with caplog.at_level(logging.WARNING):
        for filing in rejected:
            assert db.upsert_filing(conn, filing) == ""
        assert (
            db.upsert_transaction(conn, _txn(member_id="Z9001", line_index=1)) is None
        )
        assert db.upsert_transaction(conn, _txn(source_url="https://example.com/t")) is None
    assert conn.records == []
    text = caplog.text.lower()
    assert "example.com" in text
    assert "example.org" in text
    assert "member" in text
    assert "z9" in text or "placeholder" in text
    assert "year" in text

    kept = db.upsert_filing(conn, _filing())
    assert kept
    db.upsert_transaction(conn, _txn(filing_id=kept))
    assert len([r for r in conn.records if r["_collection"] == "transactions"]) == 1


def test_horizons_payload_skips_bad_filing_and_transaction(monkeypatch, caplog):
    hz = HorizonsIngestClient("https://example.test", "not-a-real-key")
    posted = []

    def fake_post(body, path="/hcgi/api/ingest/disclosures"):
        posted.append(body)
        return _Resp(200, {"filing_id": "abc", "transactions_written": 1})

    monkeypatch.setattr(hz, "_post_ingest", fake_post)
    with caplog.at_level(logging.WARNING):
        skipped = hz.submit_filing(
            _filing(source_url="https://www.example.com/ptr.pdf"),
            transactions=[_txn()],
        )
    assert skipped.get("skipped") is True
    assert posted == []

    hz.submit_filing(
        _filing(),
        transactions=[
            _txn(line_index=0, member_id="test"),
            _txn(line_index=1),
        ],
    )
    assert len(posted) == 1
    rows = posted[0]["payload"]["transactions"]
    assert len(rows) == 1
    assert rows[0]["line_index"] == 1


def test_pytest_cannot_post_to_production(monkeypatch):
    hz = HorizonsIngestClient("https://app.example-prod.test", "not-a-real-key")
    # example-prod.test is not a fixture host. This process is pytest.
    monkeypatch.setenv("INGEST_LIVE", "1")

    def explode(*_args, **_kwargs):
        raise AssertionError("posted to a non-fixture host from tests")

    monkeypatch.setattr(hz.session, "post", explode)
    with pytest.raises(RuntimeError, match="production|dry-run|live"):
        hz._post_ingest({"type": "filing", "payload": {}})


def test_production_post_requires_live_opt_in(monkeypatch):
    hz = HorizonsIngestClient("https://app.example-prod.test", "not-a-real-key")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("INGEST_LIVE", raising=False)
    called = {"n": 0}

    def fake_post(*_args, **_kwargs):
        called["n"] += 1
        return _Resp(200, {"ok": True})

    monkeypatch.setattr(hz.session, "post", fake_post)
    with pytest.raises(RuntimeError, match="INGEST_LIVE|--live"):
        hz._post_ingest({"type": "filing", "payload": {}})
    assert called["n"] == 0

    monkeypatch.setenv("INGEST_LIVE", "1")
    hz._post_ingest({"type": "filing", "payload": {}})
    assert called["n"] == 1
