"""House PTR text parsing: tickers and wrapped amount rows."""
from ingest.house import _ocr_page, extract_ticker, parse_ptr_transactions


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


def test_ocr_failure_is_empty_text():
    class BrokenPage:
        def to_image(self, resolution=200):
            raise RuntimeError("no rasterizer")

    assert _ocr_page(BrokenPage()) == ""
