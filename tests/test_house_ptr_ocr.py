"""Scanned House PTR tables: column geometry, spacing, and real filings."""
from pathlib import Path

from ingest.house import _transactions_from_scanned_ptr, resolve_ptr_transactions
from ingest.house_ptr_ocr import (
    AMOUNT_RANGES,
    _clean_asset,
    _plausible_asset,
    columns_from_vline_centers,
    parse_ptr_date,
    restore_asset_spacing,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "ptr"

# Vertical ruling lines measured on the 2026 scans (120 dpi page widths).
_FLEISCHMANN_LINES = [
    132, 195, 544, 577, 609, 641, 724, 809, 853, 898, 943, 988, 1033, 1078,
    1123, 1169, 1213, 1258,
]
_WIED_LINES = [
    182, 215, 439, 476, 510, 545, 580, 649, 729, 772, 814, 858, 900, 942, 984,
    1026, 1069, 1112, 1155, 1197,
]
_MCCAUL_LINES = [
    25, 105, 673, 703, 732, 766, 878, 973, 1001, 1029, 1058, 1087, 1115, 1143,
    1171, 1199, 1228, 1256, 1285,
]


def test_glued_asset_names_regain_word_spaces():
    assert restore_asset_spacing(
        "BLACKSTONEPRIVATEEQUITYSTRATEGIESFUNDLPCLASS"
    ) == "BLACKSTONE PRIVATE EQUITY STRATEGIES FUND LP CLASS"
    assert restore_asset_spacing(
        "KKRPRIVATEEQUITYCONGLOMERATELLCCLASSI"
    ) == "KKR PRIVATE EQUITY CONGLOMERATE LLC CLASS I"
    # Already spaced, or too short to split, stays put.
    assert restore_asset_spacing("Apple Inc (AAPL)") == "Apple Inc (AAPL)"
    assert restore_asset_spacing("ADOBE INC") == "ADOBE INC"


def test_class_share_letter_and_leading_mark():
    assert _clean_asset(
        "BLACKSTONE PRIVATE EQUITY STRATEGIES FUND LP CLASS !"
    )[1] == "BLACKSTONE PRIVATE EQUITY STRATEGIES FUND LP CLASS I"
    assert _clean_asset("BLACKSTONEPRIVATEEQUITYSTRATEGIESFUNDLPCLASS!")[1] == (
        "BLACKSTONE PRIVATE EQUITY STRATEGIES FUND LP CLASS I"
    )
    assert _clean_asset("1 ARKANSAS ST GO REF")[1] == "ARKANSAS ST GO REF"
    assert _clean_asset(")Microsoft Corporation (MSFT)")[1].startswith("Microsoft")


def test_form_instructions_and_sample_row_are_not_transactions():
    rejected = [
        "File an original and 2 copies",
        "Example Mega Corp Common Stock",
        "Ex mple Meg Corp Comm Stock",
        "Ex mpt Mg Cop Com nStock",
        "E mp! Meg Com Comm Stock",
        "Nothing to report for July 2026",
        "File an onginal and 2 copies",
        "answered yes to this question please contact the committee",
        "Did you purchase any shares that were allocated as a part of an IPO",
        "COMMON STOCK",
        "CORPORATION CMN CLASS B",
        "Please see the attached schedule",
        "200 penalty shail be assessed against anyone who files late",
        "ICLASS A",
    ]
    for name in rejected:
        assert _plausible_asset(_clean_asset(name)[1]) is False, name
    assert _plausible_asset("HONEYWELL INTL INC") is True
    assert _plausible_asset("GlobalXFds Global X Defense Tech ETF SHLD") is True


def test_scan_dates_accept_two_digit_years_and_digit_runs():
    assert parse_ptr_date("07/28/26") == "07/28/2026"
    assert parse_ptr_date("8/20/2026") == "08/20/2026"
    assert parse_ptr_date("72426") == "07/24/2026"
    assert parse_ptr_date("o7/28/26") == "07/28/2026"
    assert parse_ptr_date("16/9/26") == "06/09/2026"
    assert parse_ptr_date("11/09/2026") == "11/09/2026"
    assert parse_ptr_date("01/11/2010") == "01/11/2010"
    assert parse_ptr_date("02/31/2026") is None
    assert parse_ptr_date("MM/DD/YY") is None
    assert parse_ptr_date("not a date") is None


def test_amount_columns_follow_the_date_column():
    for centers, width, type_count in (
        (_FLEISCHMANN_LINES, 1320, 3),
        (_WIED_LINES, 1320, 4),
        (_MCCAUL_LINES, 1320, 3),
    ):
        columns = columns_from_vline_centers(centers, width)
        assert columns is not None
        assert len(columns["amounts"]) == 10
        assert len(columns["dates"]) == 2
        assert len(columns["types"]) == type_count
        assert columns["asset"][1] - columns["asset"][0] > width * 0.10
    # Continuation page: a double-printed rule and one wide amount column.
    khanna = columns_from_vline_centers(
        [
            34, 74, 259, 293, 313, 348, 382, 425, 484, 544, 582, 620, 653, 685,
            717, 755, 792, 834, 912, 954, 1045,
        ],
        1100,
    )
    assert khanna is not None
    assert len(khanna["types"]) == 4
    assert len(khanna["dates"]) == 2
    assert len(khanna["amounts"]) == 10
    assert khanna["amount_trust"] < 10
    assert columns_from_vline_centers([10, 20, 30], 1000) is None


def test_printed_dollar_range_and_two_digit_date_parse_from_ocr_text():
    rows = _transactions_from_scanned_ptr(
        "GlobalXFds GlobalXDefense Tech ETF SHLD 07/28/26 08/01/26 $1,001 - $15,000"
    )
    assert len(rows) == 1
    assert rows[0]["transaction_date"] == "07/28/2026"
    assert rows[0]["notification_date"] == "08/01/2026"
    assert rows[0]["amount_range_raw"] == "$1,001 - $15,000"
    assert rows[0]["filer_relation"] == "self"
    assert "Defense" in rows[0]["asset_name"] or "GlobalX" in rows[0]["asset_name"]
    assert _transactions_from_scanned_ptr("Nothing to report for July 2026") == []
    assert _transactions_from_scanned_ptr("not a transaction table") == []


def test_marked_checkbox_row_keeps_amount_owner_and_blank_amount():
    encoded = (
        "@@PTR@@\tspouse\tpurchase\t08/20/2026\t09/05/2026\t"
        "$500,001 - $1,000,000\tBLACKSTONE PRIVATE EQUITY STRATEGIES FUND LP CLASS I\n"
        "@@PTR@@\tself\tother\t07/24/2026\t\t\tADOBE INC"
    )
    rows = _transactions_from_scanned_ptr(encoded)
    assert len(rows) == 2
    assert rows[0]["filer_relation"] == "spouse"
    assert rows[0]["amount_range_raw"] == "$500,001 - $1,000,000"
    assert rows[0]["transaction_type"] == "purchase"
    assert rows[0]["notification_date"] == "09/05/2026"
    assert rows[1]["amount_range_raw"] == ""
    assert rows[1]["filer_relation"] == "self"
    assert "notification_date" not in rows[1]
    result = resolve_ptr_transactions("no text layer", ocr=lambda: encoded)
    assert result["parse_status"] == "parsed"
    assert result["transactions"][0]["asset_name"].startswith("BLACKSTONE PRIVATE")


def _parsed(doc_id: str):
    from ingest.house import ocr_pdf_bytes

    content = (_FIXTURES / f"{doc_id}.pdf").read_bytes()
    text = ocr_pdf_bytes(content)
    return resolve_ptr_transactions("", ocr=lambda t=text: t)


def test_fleischmann_wied_mann_and_honeywell_scans_parse():
    fleisch = _parsed("9116290")
    assert fleisch["parse_status"] == "parsed"
    assert len(fleisch["transactions"]) >= 5
    names = " ".join(row["asset_name"] for row in fleisch["transactions"])
    assert "SHLD" in names or "Defense" in names
    assert "Example" not in names
    assert "original" not in names.lower()
    assert any(row["transaction_date"] == "07/28/2026" for row in fleisch["transactions"])
    assert any(row["amount_range_raw"] in AMOUNT_RANGES for row in fleisch["transactions"])

    wied = _parsed("9116260")
    assert wied["parse_status"] == "parsed"
    wied_names = " ".join(row["asset_name"].upper() for row in wied["transactions"])
    assert "ADOBE" in wied_names
    assert "SALESFORCE" in wied_names
    assert any(row["amount_range_raw"] in AMOUNT_RANGES for row in wied["transactions"])

    mann = _parsed("9116292")
    assert mann["parse_status"] == "parsed"
    tickers = {row["ticker"] for row in mann["transactions"]}
    assert "AAPL" in tickers
    assert "GOOGL" in tickers or any(
        "ALPHABET" in row["asset_name"].upper() for row in mann["transactions"]
    )
    assert any(row["filer_relation"] == "spouse" for row in mann["transactions"])

    honey = _parsed("9116257")
    assert honey["parse_status"] == "parsed"
    assert any("HONEYWELL" in row["asset_name"].upper() for row in honey["transactions"])
    assert all("Example" not in row["asset_name"] for row in honey["transactions"])
    assert all("Mega" not in row["asset_name"] for row in honey["transactions"])


def test_rogers_nothing_to_report_stays_unparsed():
    result = _parsed("9116311")
    assert result["transactions"] == []
    assert result["parse_status"] == "failed"
    assert "transaction" in result["parse_failure_reason"].lower()
