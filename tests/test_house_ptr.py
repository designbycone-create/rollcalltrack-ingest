"""House PTR text parsing: tickers and wrapped amount rows."""
import re

from ingest import db
from ingest.house import _ocr_page, extract_ticker, parse_ptr_transactions
from ingest.ingest_client import HorizonsIngestClient

# Hern PTR 20035588 lines 47 and 51: same owner, asset, dates, and amount.
_IDENTICAL_LMT_LINES = """\
JT Lockheed Martin Corporation S (partial) 09/17/2026 10/02/2026 $15,001 -
Common Stock (LMT) [ST] $50,000
JT Lockheed Martin Corporation S (partial) 09/17/2026 10/02/2026 $15,001 -
Common Stock (LMT) [ST] $50,000
"""


def test_extract_ticker_from_house_asset_line():
    assert extract_ticker("AT&T Inc. (T) [ST]") == ("T", "medium")
    assert extract_ticker("Berkshire Hathaway Inc. New Common Stock (BRK.B)") == (
        "BRK.B",
        "medium",
    )
    assert extract_ticker("U.S. Treasury Bill") == (None, "none")


def test_wrapped_maturity_date_still_parses():
    # Vince Fong 20035539.pdf splits the amount around a maturity date.
    text = """
U.S. Treasury Bill 0.0% Int Mat P 09/18/2026 09/18/2026 $15,001 -
12/10/2026 [GS] $50,000
Alphabet Inc. - Class C Capital Stock (GOOG) S 10/16/2026 10/16/2026 $1,001 - $15,000
"""
    txns = parse_ptr_transactions(text)
    assert len(txns) == 2
    assert txns[0]["amount_range_raw"] == "$15,001 - $50,000"
    assert "12/10/2026" in txns[0]["asset_name"]
    assert txns[0]["ticker"] is None
    assert txns[1]["ticker"] == "GOOG"
    assert txns[1]["ticker_confidence"] == "medium"


def test_ordinary_wrapped_amount_still_parses():
    text = "JT Datadog, Inc. - Class A Common P 09/28/2026 10/01/2026 $50,001 -\nStock (DDOG) [ST] $100,000\n"
    txns = parse_ptr_transactions(text)
    assert len(txns) == 1
    assert txns[0]["ticker"] == "DDOG"
    assert txns[0]["amount_range_raw"] == "$50,001 - $100,000"
    assert txns[0]["filer_relation"] == "joint"


class _MemoryPB:
    """In-memory PocketBase that honors the upsert filter."""

    def __init__(self):
        self.records: list[dict] = []
        self._seq = 0

    def find_one(self, collection: str, filt: str):
        for rec in self.records:
            if rec["_collection"] == collection and _filter_matches(rec, filt):
                return rec
        return None

    def create(self, collection: str, body: dict) -> dict:
        self._seq += 1
        rec = {"_collection": collection, "id": f"r{self._seq}", **body}
        self.records.append(rec)
        return rec

    def update(self, collection: str, record_id: str, body: dict) -> dict:
        for rec in self.records:
            if rec["id"] == record_id and rec["_collection"] == collection:
                rec.update(body)
                return rec
        raise KeyError(record_id)


def _filter_matches(rec: dict, filt: str) -> bool:
    for clause in filt.split("&&"):
        match = re.fullmatch(
            r'\s*(\w+)\s*=\s*(?:"((?:\\.|[^"\\])*)"|(-?\d+))\s*',
            clause,
        )
        assert match, clause
        field = match.group(1)
        if match.group(2) is not None:
            expected = match.group(2).replace('\\"', '"').replace("\\\\", "\\")
            actual = rec.get(field)
            if str("" if actual is None else actual) != expected:
                return False
        else:
            actual = rec.get(field)
            if actual != int(match.group(3)) and str(actual) != match.group(3):
                return False
    return True


def test_identical_lines_in_one_filing_are_both_stored():
    txns = parse_ptr_transactions(_IDENTICAL_LMT_LINES)
    assert len(txns) == 2
    conn = _MemoryPB()
    for txn in txns:
        db.upsert_transaction(conn, {**txn, "filing_id": "20035588"})
    stored = [r for r in conn.records if r["_collection"] == "transactions"]
    assert len(stored) == 2
    assert [r["line_index"] for r in stored] == [0, 1]
    assert all(r["ticker"] == "LMT" for r in stored)
    assert all(r["ticker_confidence"] == "medium" for r in stored)
    db.upsert_transaction(conn, {**txns[0], "filing_id": "20035588"})
    stored = [r for r in conn.records if r["_collection"] == "transactions"]
    assert len(stored) == 2


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


def test_horizons_payload_includes_line_index_and_ticker(monkeypatch):
    hz = HorizonsIngestClient("https://example.test", "not-a-real-key")
    seen = {}

    def fake_post(body, path="/hcgi/api/ingest/disclosures"):
        seen["body"] = body
        return _Resp(200, {"filing_id": "abc", "transactions_written": 2})

    monkeypatch.setattr(hz, "_post_ingest", fake_post)
    txns = parse_ptr_transactions(_IDENTICAL_LMT_LINES)
    hz.submit_filing(
        {"source_url": "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/20035588.pdf"},
        transactions=txns,
    )
    rows = seen["body"]["payload"]["transactions"]
    assert [row["line_index"] for row in rows] == [0, 1]
    assert rows[0]["ticker"] == "LMT"
    assert rows[1]["ticker"] == "LMT"
    assert rows[0]["ticker_confidence"] == "medium"
    assert rows[1]["ticker_confidence"] == "medium"


def test_filing_id_reingests_one_doc_outside_the_date_window(monkeypatch):
    from ingest import house

    records = [
        {
            "name": "Kevin Hern",
            "state_district": "OK01",
            "filing_type_code": "P",
            "filing_type": "Periodic Transaction Report",
            "filing_date": "1/15/2020",
            "year": "2026",
            "doc_id": "20035588",
            "pdf_url": "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/20035588.pdf",
        },
        {
            "name": "Someone Else",
            "state_district": "TX01",
            "filing_type_code": "P",
            "filing_type": "Periodic Transaction Report",
            "filing_date": "10/20/2026",
            "year": "2026",
            "doc_id": "20039999",
            "pdf_url": "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/20039999.pdf",
        },
    ]
    seen = []

    monkeypatch.setattr(house, "download_bulk_index", lambda year, session=None: records)
    monkeypatch.setattr(house.config, "REQUEST_DELAY_SECONDS", 0)

    def fake_pdf(url, session=None):
        seen.append(url)
        return {
            "text": _IDENTICAL_LMT_LINES,
            "used_ocr": False,
            "page_count": 1,
            "empty_pages": [],
        }

    monkeypatch.setattr(house, "parse_pdf", fake_pdf)
    result = house.run(
        year=2026,
        dry_run=True,
        days_back=90,
        max_filings=1,
        filing_id="20035588",
    )
    assert seen == [records[0]["pdf_url"]]
    assert result["parsed"] == 1
    assert result["found"] == 1


def test_unknown_filing_id_fails(monkeypatch):
    from ingest import house

    monkeypatch.setattr(house, "download_bulk_index", lambda year, session=None: [])
    monkeypatch.setattr(house.config, "REQUEST_DELAY_SECONDS", 0)
    result = house.run(year=2026, dry_run=True, filing_id="20035588")
    assert result["failed"] == 1
    assert result["parsed"] == 0
    assert result["found"] == 0


def test_cli_filing_id_is_passed_to_house(monkeypatch):
    import sys

    import ingest.main as mainmod

    seen = {}

    def house_run(*_a, **kwargs):
        seen.update(kwargs)
        return {"found": 1, "parsed": 1, "failed": 0}

    monkeypatch.setattr(mainmod.house, "run", house_run)
    monkeypatch.setattr(
        sys,
        "argv",
        ["ingest.main", "--source", "house", "--dry-run", "--filing-id", "20035588", "--year", "2026"],
    )
    mainmod.main()
    assert seen["filing_id"] == "20035588"
    assert seen["year"] == 2026
    assert seen["dry_run"] is True


def test_ocr_failure_is_empty_text():
    class BrokenPage:
        def to_image(self, resolution=200):
            raise RuntimeError("no rasterizer")

    assert _ocr_page(BrokenPage()) == ""
