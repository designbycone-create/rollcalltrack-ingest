"""House PTR text parsing: tickers and wrapped amount rows."""
import re
from datetime import date

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


def test_unmatched_reingest_keeps_member_id_and_party(monkeypatch):
    url = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/20035588.pdf"
    conn = _MemoryPB()
    filing_id = db.upsert_filing(
        conn,
        {
            "source_url": url,
            "member_id": "hern-id",
            "member_party": "R",
            "member_name": "Kevin Hern",
            "chamber": "house",
            "filing_type": "ptr",
            "filing_date": "10/13/2026",
        },
    )
    again = db.upsert_filing(
        conn,
        {
            "source_url": url,
            "member_id": "",
            "member_party": "",
            "member_name": "Kevin Hern",
            "chamber": "house",
            "filing_type": "ptr",
            "filing_date": "10/13/2026",
        },
    )
    assert again == filing_id
    stored = next(r for r in conn.records if r["_collection"] == "disclosure_filings")
    assert stored["member_id"] == "hern-id"
    assert stored["member_party"] == "R"

    db.upsert_transaction(
        conn,
        {
            "filing_id": filing_id,
            "line_index": 0,
            "asset_name": "Lockheed Martin Corporation Common Stock (LMT)",
            "transaction_date": "09/17/2026",
            "amount_range_raw": "$15,001 - $50,000",
            "ticker": "LMT",
            "ticker_confidence": "medium",
            "member_id": "",
        },
    )
    txn = next(r for r in conn.records if r["_collection"] == "transactions")
    assert "member_id" not in txn

    hz = HorizonsIngestClient("https://example.test", "not-a-real-key")
    seen = {}

    def fake_post(body, path="/hcgi/api/ingest/disclosures"):
        seen["body"] = body
        return _Resp(200, {"filing_id": filing_id, "transactions_written": 0})

    monkeypatch.setattr(hz, "_post_ingest", fake_post)
    hz.submit_filing(
        {
            "source_url": url,
            "member_id": "",
            "member_party": "",
            "member_name": "Kevin Hern",
        }
    )
    payload = seen["body"]["payload"]["filing"]
    assert "member_id" not in payload
    assert "member_party" not in payload
    assert payload["member_name"] == "Kevin Hern"


def test_horizons_payload_includes_line_index_and_ticker(monkeypatch):
    hz = HorizonsIngestClient("https://example.test", "not-a-real-key")
    seen = {}

    def fake_post(body, path="/hcgi/api/ingest/disclosures"):
        seen["body"] = body
        return _Resp(200, {"filing_id": "abc", "transactions_written": 2})

    monkeypatch.setattr(hz, "_post_ingest", fake_post)
    txns = parse_ptr_transactions(_IDENTICAL_LMT_LINES)
    hz.submit_filing(
        {
            "source_url": "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/20035588.pdf",
            "member_name": "Kevin Hern",
            "filing_date": date.today().isoformat(),
        },
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


_PTR_LINE = (
    "JT Apple Inc. (AAPL) P 09/01/2026 09/02/2026 $1,001 - $15,000\n"
)


def test_text_with_transactions_does_not_ocr():
    from ingest.house import resolve_ptr_transactions

    def explode():
        raise AssertionError("OCR ran even though the text layer had a transaction")

    result = resolve_ptr_transactions(_PTR_LINE, ocr=explode)
    assert result["used_ocr"] is False
    assert result["parse_status"] == "parsed"
    assert result["parse_failure_reason"] == ""
    assert result["transactions"][0]["ticker"] == "AAPL"


def test_no_transactions_falls_back_to_ocr():
    from ingest.house import resolve_ptr_transactions

    seen = {"n": 0}

    def ocr():
        seen["n"] += 1
        return _PTR_LINE

    result = resolve_ptr_transactions("scanned page with no trade rows", ocr=ocr)
    assert seen["n"] == 1
    assert result["used_ocr"] is True
    assert result["parse_status"] == "parsed"
    assert result["parse_failure_reason"] == ""
    assert result["transactions"][0]["ticker"] == "AAPL"


def test_ocr_failure_keeps_parse_status_failed_with_reason():
    from ingest.house import resolve_ptr_transactions

    empty = resolve_ptr_transactions("no rows", ocr=lambda: "   ")
    assert empty["parse_status"] == "failed"
    assert empty["transactions"] == []
    assert empty["used_ocr"] is True
    assert "ocr" in empty["parse_failure_reason"].lower()

    garbage = resolve_ptr_transactions("no rows", ocr=lambda: "not a transaction table")
    assert garbage["parse_status"] == "failed"
    assert "transaction" in garbage["parse_failure_reason"].lower()


def test_filing_id_reparse_records_ocr_failure(monkeypatch, caplog):
    import logging

    from ingest import house

    records = [
        {
            "name": "David J. Taylor",
            "state_district": "OH02",
            "filing_type_code": "P",
            "filing_type": "Periodic Transaction Report",
            "filing_date": "10/01/2026",
            "year": "2026",
            "doc_id": "9116378",
            "pdf_url": "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/9116378.pdf",
        }
    ]
    monkeypatch.setattr(house, "download_bulk_index", lambda year, session=None: records)
    monkeypatch.setattr(house.config, "REQUEST_DELAY_SECONDS", 0)
    monkeypatch.setattr(
        house,
        "parse_pdf",
        lambda url, session=None: {
            "text": "image only filing",
            "used_ocr": False,
            "page_count": 2,
            "empty_pages": 2,
            "content": b"%PDF-1.4",
        },
    )
    monkeypatch.setattr(house, "ocr_pdf_bytes", lambda content: "")

    class FakeHz:
        configured = True

        def __init__(self):
            self.filing = None
            self.flagged = None

        def lookup_member_by_name(self, *args, **kwargs):
            return {
                "id": "pb-taylor",
                "bioguide_id": "T000490",
                "name": "David Taylor",
                "party": "R",
                "state": "Ohio",
            }

        def submit_filing(self, filing, transactions=None, **kwargs):
            self.filing = filing
            self.transactions = transactions
            return {"filing_id": "f-scan", "transactions_written": 0, "updated": True}

        def submit_ingestion_log(self, *args, **kwargs):
            return None

        def flag_manual_review(self, filing_id, reason="", notes=""):
            self.flagged = {"filing_id": filing_id, "reason": reason, "notes": notes}
            return {}

    hz = FakeHz()
    with caplog.at_level(logging.INFO):
        result = house.run(
            year=2026,
            dry_run=False,
            filing_id="9116378",
            client=hz,
        )
    assert result["parsed"] == 1
    assert hz.filing["parse_status"] == "failed"
    assert "ocr" in hz.filing["parse_failure_reason"].lower()
    assert hz.transactions == []
    assert hz.filing["member_id"] == "pb-taylor"
    assert hz.flagged["reason"] == "low_ocr_confidence"
    assert "ocr" in caplog.text.lower()


def test_scanned_ptr_row_keeps_owner_asset_and_date():
    from ingest.house import _transactions_from_scanned_ptr, resolve_ptr_transactions

    line = (
        "| SP] BLACKSTONEPRIVATEEQUITYSTRATEGIESFUNDLPCLASS!, CX | | "
        "8/20/2026 | o/sjzoze | | [x]"
    )
    txns = _transactions_from_scanned_ptr(line)
    assert len(txns) == 1
    assert "BLACKSTONE" in txns[0]["asset_name"].upper()
    assert txns[0]["transaction_date"] == "08/20/2026"
    assert txns[0]["filer_relation"] == "spouse"
    assert txns[0]["amount_range_raw"] == ""
    assert txns[0]["transaction_type"] == "other"

    typed = _transactions_from_scanned_ptr("SP Apple Inc (AAPL) P 9/1/2026")
    assert typed[0]["transaction_type"] == "purchase"
    assert typed[0]["ticker"] == "AAPL"
    assert typed[0]["transaction_date"] == "09/01/2026"

    assert _transactions_from_scanned_ptr("not a transaction table") == []
    result = resolve_ptr_transactions("no text layer", ocr=lambda: line)
    assert result["parse_status"] == "parsed"
    assert result["used_ocr"] is True
    assert "BLACKSTONE" in result["transactions"][0]["asset_name"].upper()


def test_ocr_failure_logs_a_text_sample(caplog):
    import logging

    from ingest.house import resolve_ptr_transactions

    with caplog.at_level(logging.WARNING):
        result = resolve_ptr_transactions(
            "no rows", ocr=lambda: "not a transaction table"
        )
    assert result["parse_status"] == "failed"
    assert "ocr_sample=" in caplog.text
    assert "not a transaction table" in caplog.text


def test_ocr_renders_at_300_and_corrects_osd_rotation(monkeypatch):
    import sys
    import types

    from ingest.house import _ocr_with_pdf2image

    seen = {}

    class FakeImage:
        def convert(self, mode):
            seen["mode"] = mode
            return self

        def rotate(self, angle, expand=False):
            seen["rotate"] = (angle, expand)
            return self

    image = FakeImage()

    pdf2image = types.ModuleType("pdf2image")

    def convert_from_bytes(content, dpi=200):
        seen["dpi"] = dpi
        seen["content"] = content
        return [image]

    pdf2image.convert_from_bytes = convert_from_bytes

    pytesseract = types.ModuleType("pytesseract")

    def image_to_osd(_image):
        return "Orientation in degrees: 90\nRotate: 270\nOrientation confidence: 5.10"

    def image_to_string(_image, config=""):
        seen["config"] = config
        return "NAME MICHAEL MCCAUL"

    pytesseract.image_to_osd = image_to_osd
    pytesseract.image_to_string = image_to_string

    pil_imageops = types.ModuleType("ImageOps")

    def autocontrast(img):
        seen["autocontrast"] = True
        return img

    pil_imageops.autocontrast = autocontrast
    pil = types.ModuleType("PIL")
    pil.ImageOps = pil_imageops

    monkeypatch.setitem(sys.modules, "pdf2image", pdf2image)
    monkeypatch.setitem(sys.modules, "pytesseract", pytesseract)
    monkeypatch.setitem(sys.modules, "PIL", pil)
    monkeypatch.setitem(sys.modules, "PIL.ImageOps", pil_imageops)

    text = _ocr_with_pdf2image(b"%PDF")
    assert "MICHAEL MCCAUL" in text
    assert seen["dpi"] == 300
    assert seen["rotate"] == (-270, True)
    assert seen["autocontrast"] is True
    assert seen["config"] == "--psm 6"


def test_mccaul_landscape_scan_yields_transactions():
    from pathlib import Path

    from ingest.house import ocr_pdf_bytes, resolve_ptr_transactions

    content = (Path(__file__).parent / "fixtures" / "9116378.pdf").read_bytes()
    result = resolve_ptr_transactions("", ocr=lambda: ocr_pdf_bytes(content))
    assert result["used_ocr"] is True
    assert result["parse_status"] == "parsed"
    assert result["transactions"]
    blob = " ".join(txn["asset_name"] for txn in result["transactions"]).upper()
    dates = {txn["transaction_date"] for txn in result["transactions"]}
    assert "BLACKSTONE" in blob or "08/20/2026" in dates
