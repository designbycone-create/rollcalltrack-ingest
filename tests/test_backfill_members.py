"""Backfill member_id and party on filings already stored in Horizons."""
from ingest.backfill_members import run
from ingest.ingest_client import HorizonsIngestClient


class _Resp:
    def __init__(self, status, data):
        self.status_code = status
        self._data = data
        self.text = str(data)

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


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


def test_live_post_resends_every_trade_with_identity_fields(monkeypatch):
    """The disclosures write replaces transactions, so every stored trade goes back."""
    hz = HorizonsIngestClient("https://example.test", "not-a-real-key")
    listed = [
        _txn(
            id="t0",
            line_index=0,
            ticker="",
            ticker_confidence="",
            member_id="",
            member_party="",
        ),
        _txn(
            id="t1",
            line_index=1,
            ticker="MSFT",
            ticker_confidence="high",
            asset_name="Microsoft Corp. (MSFT)",
            member_id="61kvydvhn8votlk",
            member_party="R",
        ),
    ]
    filing = _filing(
        transactions=[
            _txn(id="t0", line_index=0, ticker="AAPL", ticker_confidence="medium"),
            _txn(
                id="t2",
                line_index=2,
                ticker="",
                ticker_confidence="none",
                asset_name="U.S. Treasury Bill",
            ),
        ]
    )
    posted = {}

    def fake_post(body, path="/hcgi/api/ingest/disclosures"):
        posted["body"] = body
        return _Resp(200, {"filing_id": "f1", "transactions_written": 3, "updated": True})

    monkeypatch.setattr(hz, "_post_ingest", fake_post)
    monkeypatch.setattr(hz, "list_disclosure_filings", lambda: [filing])
    monkeypatch.setattr(hz, "list_disclosure_transactions", lambda: listed)
    monkeypatch.setattr(
        hz,
        "lookup_member_by_name",
        lambda *a, **k: {
            "id": "61kvydvhn8votlk",
            "bioguide_id": "T000490",
            "name": "David Taylor",
            "party": "R",
            "state": "Ohio",
        },
    )

    result = run(client=hz, live=True)
    assert result["dry_run"] is False
    payload = posted["body"]["payload"]
    assert payload["filing"]["member_id"] == "61kvydvhn8votlk"
    rows = payload["transactions"]
    assert [row["line_index"] for row in rows] == [0, 1, 2]
    by_line = {row["line_index"]: row for row in rows}
    assert by_line[0]["ticker"] == "AAPL"
    assert by_line[0]["ticker_confidence"] == "medium"
    assert by_line[0]["member_id"] == "61kvydvhn8votlk"
    assert by_line[1]["ticker"] == "MSFT"
    assert by_line[1]["ticker_confidence"] == "high"
    assert by_line[1]["member_id"] == "61kvydvhn8votlk"
    assert by_line[2]["ticker"] == ""
    assert by_line[2]["ticker_confidence"] == "none"
    assert by_line[2]["asset_name"] == "U.S. Treasury Bill"
    assert by_line[2]["member_id"] == "61kvydvhn8votlk"


def test_live_post_never_sends_a_bioguide_as_member_id(monkeypatch):
    hz = HorizonsIngestClient("https://example.test", "not-a-real-key")
    trades = [
        _txn(
            id="t0",
            line_index=0,
            ticker="AAPL",
            ticker_confidence="medium",
            member_id="T000490",
        ),
        _txn(
            id="t1",
            line_index=1,
            ticker="MSFT",
            ticker_confidence="high",
            asset_name="Microsoft Corp. (MSFT)",
            member_id="61kvydvhn8votlk",
            member_party="R",
        ),
        _txn(
            id="t2",
            line_index=2,
            ticker="GOOGL",
            ticker_confidence="low",
            asset_name="Alphabet Inc. (GOOGL)",
            member_id="",
        ),
    ]
    posted = {}

    def fake_post(body, path="/hcgi/api/ingest/disclosures"):
        posted["body"] = body
        return _Resp(200, {"filing_id": "f1", "transactions_written": 3, "updated": True})

    monkeypatch.setattr(hz, "_post_ingest", fake_post)
    monkeypatch.setattr(
        hz,
        "list_disclosure_filings",
        lambda: [_filing(member_id="T000490", transactions=trades)],
    )
    monkeypatch.setattr(hz, "list_disclosure_transactions", lambda: trades)
    monkeypatch.setattr(
        hz,
        "lookup_member_by_name",
        lambda *a, **k: {
            "id": "T000490",
            "member_id": "T000490",
            "bioguide_id": "T000490",
            "name": "David Taylor",
            "party": "R",
            "state": "Ohio",
        },
    )

    run(client=hz, live=True)
    payload = posted["body"]["payload"]
    filing = payload["filing"]
    assert "member_id" not in filing
    assert filing["bioguide_id"] == "T000490"
    assert filing["member_party"] == "R"
    rows = payload["transactions"]
    assert [row["line_index"] for row in rows] == [0, 1, 2]
    by_line = {row["line_index"]: row for row in rows}
    assert "member_id" not in by_line[0]
    assert by_line[0]["ticker"] == "AAPL"
    assert by_line[0]["ticker_confidence"] == "medium"
    assert by_line[1]["member_id"] == "61kvydvhn8votlk"
    assert by_line[1]["ticker"] == "MSFT"
    assert by_line[1]["ticker_confidence"] == "high"
    assert "member_id" not in by_line[2]
    assert by_line[2]["ticker"] == "GOOGL"
    assert by_line[2]["ticker_confidence"] == "low"
    assert all(row.get("member_id") != "T000490" for row in rows)


def test_live_post_prefers_horizons_id_when_lookup_id_is_bioguide(monkeypatch):
    hz = HorizonsIngestClient("https://example.test", "not-a-real-key")
    posted = {}

    def fake_post(body, path="/hcgi/api/ingest/disclosures"):
        posted["body"] = body
        return _Resp(200, {"filing_id": "f1", "transactions_written": 1, "updated": True})

    monkeypatch.setattr(hz, "_post_ingest", fake_post)
    monkeypatch.setattr(hz, "list_disclosure_filings", lambda: [_filing(member_id="T000490")])
    monkeypatch.setattr(hz, "list_disclosure_transactions", lambda: [_txn(member_id="T000490")])
    monkeypatch.setattr(
        hz,
        "lookup_member_by_name",
        lambda *a, **k: {
            "id": "T000490",
            "member_id": "61kvydvhn8votlk",
            "bioguide_id": "T000490",
            "name": "David Taylor",
            "party": "R",
            "state": "Ohio",
        },
    )

    run(client=hz, live=True)
    payload = posted["body"]["payload"]
    assert payload["filing"]["member_id"] == "61kvydvhn8votlk"
    assert payload["filing"]["bioguide_id"] == "T000490"
    row = payload["transactions"][0]
    assert row["member_id"] == "61kvydvhn8votlk"
    assert row["line_index"] == 0
    assert row["ticker"] == "AAPL"
    assert row["ticker_confidence"] == "medium"


def test_live_does_not_replace_trades_when_none_were_listed(monkeypatch):
    hz = HorizonsIngestClient("https://example.test", "not-a-real-key")
    posted = []

    def fake_post(body, path="/hcgi/api/ingest/disclosures"):
        posted.append(body)
        return _Resp(200, {"filing_id": "f1", "transactions_written": 0})

    monkeypatch.setattr(hz, "_post_ingest", fake_post)
    monkeypatch.setattr(hz, "list_disclosure_filings", lambda: [_filing()])
    monkeypatch.setattr(hz, "list_disclosure_transactions", lambda: [])
    monkeypatch.setattr(
        hz,
        "lookup_member_by_name",
        lambda *a, **k: {
            "id": "61kvydvhn8votlk",
            "bioguide_id": "T000490",
            "party": "R",
            "name": "David Taylor",
        },
    )

    result = run(client=hz, live=True)
    assert posted == []
    assert result["unresolved"] == 1


def test_run_fails_when_the_list_route_does_not_answer():
    from ingest.ingest_client import DisclosureListError

    class _Broken:
        def list_disclosure_filings(self):
            raise DisclosureListError("GET /hcgi/api/members returned 404")

        def list_disclosure_transactions(self):
            raise AssertionError("filings list should have failed first")

    result = run(client=_Broken(), live=False)
    assert result["ok"] is False
    assert result["filings_changed"] == 0
    assert "404" in result["error"]


def test_cli_exits_when_the_list_route_does_not_answer(monkeypatch):
    import ingest.backfill_members as mod

    monkeypatch.setattr(
        mod,
        "run",
        lambda client=None, live=False: {
            "ok": False,
            "error": "GET /hcgi/api/members returned 404",
            "dry_run": True,
            "filings_changed": 0,
            "transactions_changed": 0,
            "unresolved": 0,
        },
    )
    monkeypatch.setattr(mod, "get_client", lambda: object())
    assert mod.main([]) == 1


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
