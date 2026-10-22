"""Backfill member_id and party on filings already stored in Horizons."""
from datetime import date, timedelta

from ingest.backfill_members import plan_member_updates, run
from ingest.ingest_client import HorizonsIngestClient
from tests.test_house_ptr import _Resp


def _filing(**overrides):
    body = {
        "id": "filing-taylor",
        "source_url": "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/20036001.pdf",
        "chamber": "house",
        "filing_type": "ptr",
        "filing_date": date.today().strftime("%m/%d/%Y"),
        "raw_document_type": "pdf",
        "parse_status": "parsed",
        "member_name": "David J. Taylor",
        "member_state": "OH",
        "bioguide_id": "T000490",
        "member_id": "",
        "member_party": "",
    }
    body.update(overrides)
    return body


def _txn(**overrides):
    body = {
        "id": "txn-1",
        "filing_id": "filing-taylor",
        "line_index": 0,
        "asset_name": "Apple Inc. (AAPL)",
        "transaction_type": "purchase",
        "transaction_date": "01/02/2026",
        "amount_range_raw": "$1,001 - $15,000",
        "ticker": "AAPL",
        "ticker_confidence": "medium",
        "member_id": "",
        "member_party": "",
    }
    body.update(overrides)
    return body


def _resolver(name, state, bioguide_id=None, chamber=None, office=None):
    assert state == "Ohio"
    assert bioguide_id == "T000490"
    return {
        "id": "taylor-row",
        "bioguide_id": "T000490",
        "name": "David Taylor",
        "state": "Ohio",
        "party": "R",
    }


def test_plan_fills_blank_member_fields_and_expands_state():
    planned = plan_member_updates([_filing()], [_txn()], _resolver)
    assert planned
    filing = planned[0]["filing"]
    assert filing["member_id"] == "taylor-row"
    assert filing["member_party"] == "R"
    assert filing["member_state"] == "Ohio"
    assert filing["member_name"] == "David J. Taylor"
    assert filing["bioguide_id"] == "T000490"
    txn = planned[0]["transactions"][0]
    assert txn["member_id"] == "taylor-row"
    assert txn["member_party"] == "R"
    fields = {change["field"] for change in planned[0]["changes"]}
    assert "member_id" in fields
    assert "member_party" in fields


def test_plan_does_not_overwrite_or_blank_existing_fields():
    def resolver(name, state, bioguide_id=None, chamber=None, office=None):
        return {
            "id": "other-row",
            "bioguide_id": "T000490",
            "name": "Someone Else",
            "state": "Ohio",
            "party": "",
        }

    planned = plan_member_updates(
        [_filing(member_id="keep-me", member_party="R", member_name="David Taylor")],
        [_txn(member_id="keep-me", member_party="R")],
        resolver,
    )
    assert planned == []

    planned = plan_member_updates(
        [_filing(member_id="keep-me", member_party="", member_state="Ohio")],
        [_txn(member_id="", member_party="R")],
        lambda **_kwargs: {
            "id": "other-row",
            "party": "D",
            "state": "Ohio",
            "bioguide_id": "T000490",
            "name": "David Taylor",
        },
    )
    filing = planned[0]["filing"]
    assert filing["member_id"] == "keep-me"
    assert filing["member_party"] == "D"
    assert filing["member_name"] == "David J. Taylor"
    assert planned[0]["transactions"][0]["member_id"] == "keep-me"
    assert planned[0]["transactions"][0]["member_party"] == "R"
    assert all(change["after"] for change in planned[0]["changes"])
    assert all(change["before"] in ("", None) for change in planned[0]["changes"])


def test_unresolved_filer_is_not_blanked():
    planned = plan_member_updates([_filing()], [_txn()], lambda **_kwargs: None)
    assert planned == []


def test_peters_and_lee_bioguides_are_passed_through():
    seen = []

    def resolver(name, state, bioguide_id=None, chamber=None, office=None):
        seen.append((name, state, bioguide_id))
        return {
            "id": f"row-{bioguide_id}",
            "party": "D",
            "state": state,
            "bioguide_id": bioguide_id,
            "name": name,
        }

    plan_member_updates(
        [
            _filing(
                id="filing-peters",
                member_name="Scott Peters",
                member_state="MI",
                bioguide_id="P000608",
                source_url="https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/20036002.pdf",
            ),
            _filing(
                id="filing-lee",
                member_name="Barbara Lee",
                member_state="CA",
                bioguide_id="L000597",
                source_url="https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/20036003.pdf",
            ),
        ],
        [],
        resolver,
    )
    assert ("Scott Peters", "Michigan", "P000608") in seen
    assert ("Barbara Lee", "California", "L000597") in seen


def test_dry_run_prints_and_does_not_post(monkeypatch, capsys):
    hz = HorizonsIngestClient("https://example.test", "not-a-real-key")
    posted = []

    def fake_post(body, path="/hcgi/api/ingest/disclosures"):
        posted.append(body)
        return _Resp(200, {"filing_id": "abc"})

    monkeypatch.setattr(hz, "_post_ingest", fake_post)
    monkeypatch.setattr(hz, "list_disclosure_filings", lambda: [_filing()])
    monkeypatch.setattr(hz, "list_disclosure_transactions", lambda: [_txn()])
    monkeypatch.setattr(hz, "lookup_member_by_name", lambda *a, **k: _resolver(*a, **k))
    result = run(client=hz, live=False)
    captured = capsys.readouterr().out
    assert posted == []
    assert "taylor-row" in captured
    assert "member_id" in captured
    assert result["updated"] == 0
    assert result["planned"] == 1


def test_live_posts_filled_fields_only(monkeypatch, caplog):
    hz = HorizonsIngestClient("https://example.test", "not-a-real-key")
    posted = []

    def fake_post(body, path="/hcgi/api/ingest/disclosures"):
        posted.append(body)
        return _Resp(200, {"filing_id": "filing-taylor", "transactions_written": 1, "updated": True})

    monkeypatch.setattr(hz, "_post_ingest", fake_post)
    monkeypatch.setattr(
        hz,
        "list_disclosure_filings",
        lambda: [_filing(member_id="keep-me")],
    )
    monkeypatch.setattr(hz, "list_disclosure_transactions", lambda: [_txn()])
    monkeypatch.setattr(
        hz,
        "lookup_member_by_name",
        lambda *a, **k: {
            "id": "other-row",
            "party": "R",
            "state": "Ohio",
            "bioguide_id": "T000490",
            "name": "David Taylor",
        },
    )
    import logging

    with caplog.at_level(logging.INFO):
        result = run(client=hz, live=True)
    assert result["updated"] == 1
    assert len(posted) == 1
    filing = posted[0]["payload"]["filing"]
    assert filing["member_id"] == "keep-me"
    assert filing["member_party"] == "R"
    assert filing["member_name"] == "David J. Taylor"
    assert "" not in filing.values()
    txn = posted[0]["payload"]["transactions"][0]
    assert txn["member_id"] == "keep-me"
    text = caplog.text
    assert "member_party" in text
    assert "keep-me" in text
    assert "other-row" not in filing["member_id"]


def test_live_backfill_posts_filings_older_than_a_year(monkeypatch):
    old = (date.today() - timedelta(days=500)).strftime("%m/%d/%Y")
    hz = HorizonsIngestClient("https://example.test", "not-a-real-key")
    posted = []

    def fake_post(body, path="/hcgi/api/ingest/disclosures"):
        posted.append(body)
        return _Resp(200, {"filing_id": "filing-taylor", "updated": True})

    monkeypatch.setattr(hz, "_post_ingest", fake_post)
    monkeypatch.setattr(hz, "list_disclosure_filings", lambda: [_filing(filing_date=old)])
    monkeypatch.setattr(hz, "list_disclosure_transactions", lambda: [_txn()])
    monkeypatch.setattr(hz, "lookup_member_by_name", lambda *a, **k: _resolver(*a, **k))
    result = run(client=hz, live=True)
    assert result["updated"] == 1
    assert len(posted) == 1
    assert posted[0]["payload"]["filing"]["member_id"] == "taylor-row"
    assert posted[0]["payload"]["filing"]["filing_date"] == old
