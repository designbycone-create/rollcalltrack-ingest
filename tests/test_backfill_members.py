"""Backfill member_id and party on filings already stored in Horizons."""
from ingest.backfill_members import run


def _filing(**overrides):
    body = {
        "id": "f1",
        "source_url": "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/2001.pdf",
        "member_name": "David J. Taylor",
        "member_state": "OH",
        "member_id": "",
        "member_party": "",
        "bioguide_id": "T000490",
        "chamber": "house",
        "filing_type": "ptr",
        "filing_date": "2024-01-15",
        "raw_document_type": "pdf",
        "parse_status": "parsed",
    }
    body.update(overrides)
    return body


def _txn(**overrides):
    body = {
        "id": "t1",
        "filing_id": "f1",
        "line_index": 0,
        "asset_name": "Apple Inc. (AAPL)",
        "transaction_type": "purchase",
        "transaction_date": "2024-01-02",
        "amount_range_raw": "$1,001 - $15,000",
        "ticker": "AAPL",
        "ticker_confidence": "medium",
        "member_id": "",
        "member_party": "",
    }
    body.update(overrides)
    return body


class _Client:
    def __init__(self, filings, transactions, member):
        self.filings = filings
        self.transactions = transactions
        self.member = member
        self.posted = []
        self.lookups = []

    def list_disclosure_filings(self):
        return self.filings

    def list_disclosure_transactions(self):
        return self.transactions

    def lookup_member_by_name(self, name, **kwargs):
        self.lookups.append((name, kwargs))
        return self.member

    def submit_filing(self, filing, transactions=None, **kwargs):
        self.posted.append({"filing": filing, "transactions": transactions, **kwargs})
        return {"filing_id": filing.get("id") or "f1", "transactions_written": len(transactions or [])}


def test_dry_run_prints_planned_changes_and_does_not_post(capsys):
    client = _Client(
        [_filing(), _filing(id="f-ok", member_id="already", member_party="D", member_name="Someone Else")],
        [_txn(), _txn(id="t-ok", filing_id="f-ok", member_id="already", member_party="D")],
        {
            "id": "pb-taylor",
            "bioguide_id": "T000490",
            "name": "David Taylor",
            "party": "R",
            "state": "Ohio",
        },
    )
    result = run(client=client, live=False)
    out = capsys.readouterr().out
    assert result["dry_run"] is True
    assert result["filings_changed"] == 1
    assert result["transactions_changed"] == 1
    assert client.posted == []
    assert "pb-taylor" in out
    assert "T000490" in out
    assert "f1" in out
    assert client.lookups[0][0] == "David J. Taylor"
    assert client.lookups[0][1]["state"] == "OH"
    assert client.lookups[0][1]["bioguide_id"] == "T000490"


def test_live_fills_blanks_and_keeps_existing_member_id(capsys):
    client = _Client(
        [_filing(member_id="keep-me", member_party="", bioguide_id="")],
        [
            _txn(member_id="", member_party=""),
            _txn(id="t2", line_index=1, member_id="keep-me", member_party="R", asset_name="Other"),
        ],
        {
            "id": "different-id",
            "bioguide_id": "T000490",
            "name": "David Taylor",
            "party": "R",
            "state": "Ohio",
        },
    )
    result = run(client=client, live=True)
    assert result["dry_run"] is False
    assert len(client.posted) == 1
    posted = client.posted[0]
    filing = posted["filing"]
    assert filing["member_id"] == "keep-me"
    assert filing["member_party"] == "R"
    assert filing["bioguide_id"] == "T000490"
    assert filing["member_name"] == "David J. Taylor"
    assert filing["member_state"] == "OH"
    assert posted.get("ignore_stale_date") is True
    rows = posted["transactions"]
    assert len(rows) == 2
    by_line = {row["line_index"]: row for row in rows}
    assert by_line[0]["member_id"] == "keep-me"
    assert by_line[0]["member_party"] == "R"
    assert by_line[0]["ticker"] == "AAPL"
    assert by_line[1]["member_id"] == "keep-me"
    assert by_line[1]["member_party"] == "R"
    text = capsys.readouterr().out
    assert "member_party" in text
    assert "keep-me" in text


def test_unresolved_rows_are_not_posted_or_blanked(capsys):
    original = _filing()
    client = _Client([original], [_txn()], None)
    result = run(client=client, live=True)
    assert client.posted == []
    assert result["unresolved"] == 1
    assert original["member_id"] == ""
    assert original["member_party"] == ""
    assert "unresolved" in capsys.readouterr().out.lower()


def test_cli_defaults_to_dry_run(monkeypatch):
    import ingest.backfill_members as mod

    seen = {}

    def fake_run(client=None, live=False):
        seen["live"] = live
        return {"dry_run": not live, "filings_changed": 0, "transactions_changed": 0, "unresolved": 0}

    monkeypatch.setattr(mod, "run", fake_run)
    monkeypatch.setattr(mod, "get_client", lambda: object())
    mod.main([])
    assert seen["live"] is False
    mod.main(["--live"])
    assert seen["live"] is True
