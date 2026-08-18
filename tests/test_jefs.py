"""Offline tests for AO / JEFS judicial disclosure mapping."""
from ingest.jefs import (
    _normalize_search_row,
    build_payload,
    court_compatible,
    iso_date,
    map_filing_type,
    map_txn_type,
    match_judge,
    parse_amount_range,
    parse_annual_assets,
    parse_ptr_text,
    register_form_payload,
    run,
    source_url_for,
)

ROSTER = [
    {
        "judge_id": "1377101",
        "full_name": "Samuel A. Alito Jr.",
        "court": "Supreme Court of the United States",
        "court_type": "supreme_court",
        "status": "active",
        "source_url": "https://www.fjc.gov/node/1377101",
    },
    {
        "judge_id": "13761857",
        "full_name": "Adam Ben Abelson",
        "court": "U.S. District Court for the District of Maryland",
        "court_type": "district_court",
        "status": "active",
        "source_url": "https://www.fjc.gov/node/13761857",
    },
    {
        "judge_id": "1001",
        "full_name": "John Smith",
        "court": "U.S. District Court for the District of Maryland",
        "court_type": "district_court",
        "status": "active",
        "source_url": "https://www.fjc.gov/node/1001",
    },
    {
        "judge_id": "1002",
        "full_name": "John Smith",
        "court": "U.S. Court of Appeals for the Fourth Circuit",
        "court_type": "circuit_court",
        "status": "active",
        "source_url": "https://www.fjc.gov/node/1002",
    },
]

PTR_TEXT = """
Form AO 10T Periodic Transaction Report
Name of Person Reporting: Samuel A. Alito, Jr.

Description of Asset Transaction Type Date of Transaction Date Notified Amount of Transaction
Apple Inc. (AAPL) Buy 01/15/2026 01/16/2026 $15,001 - $50,000
Microsoft Corporation Sold (part) 02/01/2026 02/03/2026 $50,001 - $100,000
Vanguard 500 Exchange 03/10/2026 03/11/2026 More than $50,000,000
"""

ANNUAL_TEXT = """
Form AO 10
Part VII. Investments and Trusts
Apple Inc. common stock A K $15,001 - $50,000
Brokerage cash account J
Part VIII. Additional Information
"""


def test_iso_date_parses_jefs_space_comma():
    assert iso_date("07 30, 2026") == "2026-07-30"
    assert iso_date("2026-07-30T12:00:00") == "2026-07-30"
    assert iso_date("07/30/2026") == "2026-07-30"


def test_normalize_search_row_uses_ao_certified_dt():
    rec = _normalize_search_row(
        {
            "id": "abc-1",
            "name_s": "Doe, Jane",
            "court_type_s": "U.S. District Court - Alaska",
            "report_type_s": "Periodic Transaction Report",
            "ao_certified_dt": "07 30, 2026",
            "operating_year_s": "2026",
        }
    )
    assert rec["filing_date"] == "2026-07-30"
    assert rec["filing_type"] == "periodic_transaction"


def test_source_url_carries_jefs_id():
    url = source_url_for("abc123")
    assert url.startswith("https://pub.jefs.uscourts.gov/")
    assert "abc123" in url


def test_filing_type_map():
    assert map_filing_type("Periodic Transaction Report") == "periodic_transaction"
    assert map_filing_type("Annual") == "annual"
    assert map_filing_type("Annual/Final") == "annual"
    assert map_filing_type("Initial") == "annual"
    assert map_filing_type("Mystery") is None


def test_txn_type_map():
    assert map_txn_type("Buy (add'l)") == "purchase"
    assert map_txn_type("Sold (part)") == "sale"
    assert map_txn_type("Redeemed") == "sale"
    assert map_txn_type("Exchange") == "exchange"
    assert map_txn_type("donated") is None


def test_amount_range_never_midpoint():
    assert parse_amount_range("$15,001 - $50,000") == (15001, 50000)
    assert parse_amount_range("$15,001 to $50,000") == (15001, 50000)
    assert parse_amount_range("K") == (15001, 50000)
    assert parse_amount_range("P4") == (50_000_001, None)
    assert parse_amount_range("More than $50,000,000") == (50_000_000, None)
    assert parse_amount_range("not a range") == (None, None)
    low, high = parse_amount_range("$15,001 - $50,000")
    assert (low + high) / 2 != low


def test_ptr_parser_uses_disclosed_brackets():
    txns = parse_ptr_text(PTR_TEXT, "fid-1", source_url_for("fid-1"))
    assert len(txns) == 3
    assert txns[0]["asset_name"].startswith("Apple")
    assert txns[0]["transaction_type"] == "purchase"
    assert txns[0]["transaction_date"] == "2026-01-15"
    assert txns[0]["amount_range_low"] == 15001
    assert txns[0]["amount_range_high"] == 50000
    assert txns[1]["transaction_type"] == "sale"
    assert txns[2]["amount_range_high"] is None
    assert all(t["source_url"] for t in txns)
    assert all(t["filing_id"] == "fid-1" for t in txns)


def test_annual_parser_part_vii_only():
    assets = parse_annual_assets(ANNUAL_TEXT, "fid-2", source_url_for("fid-2"))
    names = " ".join(a["asset_name"] for a in assets)
    assert "Apple" in names
    assert all(a["source_url"] for a in assets)
    apple = next(a for a in assets if "Apple" in a["asset_name"])
    assert apple["value_range_low"] == 15001
    assert apple["value_range_high"] == 50000


def test_match_unique_name():
    hit = match_judge("Samuel A. Alito, Jr.", "Supreme Court", ROSTER)
    assert hit["judge_id"] == "1377101"


def test_match_ambiguous_without_court_is_none():
    assert match_judge("John Smith", None, ROSTER) is None


def test_match_ambiguous_disambiguated_by_court():
    hit = match_judge(
        "John Smith",
        "U.S. Court of Appeals for the Fourth Circuit",
        ROSTER,
    )
    assert hit["judge_id"] == "1002"


def test_match_unknown_name_is_none():
    assert match_judge("No Such Judge", "District of Mars", ROSTER) is None


def test_court_compatible():
    assert court_compatible(
        "U.S. District Court - Maryland",
        "U.S. District Court for the District of Maryland",
    )
    assert not court_compatible("Fourth Circuit", "District of Maryland")


def test_run_offline_fixture_matches_and_reviews():
    rows = [
        {
            "jefs_id": "ptr-1",
            "name": "Samuel A. Alito, Jr.",
            "court": "Supreme Court of the United States",
            "report_type": "Periodic Transaction Report",
            "filing_type": "periodic_transaction",
            "filing_date": "2026-01-20",
            "source_url": source_url_for("ptr-1"),
        },
        {
            "jefs_id": "amb-1",
            "name": "John Smith",
            "court": "",
            "report_type": "Annual",
            "filing_type": "annual",
            "filing_date": "2026-05-15",
            "source_url": source_url_for("amb-1"),
        },
    ]
    result = run(
        dry_run=True,
        search_rows=rows,
        pdf_text_by_id={"ptr-1": PTR_TEXT},
        roster=ROSTER,
    )
    assert result["found"] == 2
    assert result["mapped"] == 1
    assert result["review"] == 1
    assert result["posted"] == 0


def test_build_payload_ptr():
    row = {
        "jefs_id": "ptr-1",
        "filing_type": "periodic_transaction",
        "filing_date": "2026-01-20",
        "source_url": source_url_for("ptr-1"),
    }
    payload = build_payload(row, ROSTER[0], PTR_TEXT)
    assert payload["filing"]["judge_id"] == "1377101"
    assert payload["_parse_ok"] is True
    assert len(payload["transactions"]) == 3
    assert payload["annual_assets"] == []


def test_register_form_payload_matches_live_jefs_form():
    ident = {
        "first_name": "Ada",
        "last_name": "Lovelace",
        "occupation": "Analyst",
        "email": "ada@example.com",
        "address-line-1": "10 Test Street",
        "address-city": "Mobile",
        "address-state": "Alabama",
        "address-postalcode": "36602",
        "representing": "self",
        "address-country": "United States",
    }
    payload = register_form_payload("csrf-token", ident)
    assert payload["action"] == "register"
    assert payload["certified"] == "on"
    assert payload["_csrf"] == "csrf-token"
    assert payload["first_name"] == "Ada"
    assert payload["address-state"] == "Alabama"
    assert "address-country" not in payload


if __name__ == "__main__":
    for fn in [
        test_source_url_carries_jefs_id,
        test_filing_type_map,
        test_txn_type_map,
        test_amount_range_never_midpoint,
        test_ptr_parser_uses_disclosed_brackets,
        test_annual_parser_part_vii_only,
        test_match_unique_name,
        test_match_ambiguous_without_court_is_none,
        test_match_ambiguous_disambiguated_by_court,
        test_match_unknown_name_is_none,
        test_court_compatible,
        test_run_offline_fixture_matches_and_reviews,
        test_build_payload_ptr,
    ]:
        fn()
        print("ok", fn.__name__)
    print("all passed")
