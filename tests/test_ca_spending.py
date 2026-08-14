"""Offline tests for California Open FI$Cal vendor-expenditure mapping."""
from datetime import date

from ingest.ca_spending import (
    california_fiscal_year,
    files_for_window,
    fiscal_year_bounds,
    iso_date,
    map_row,
    parse_vendor_filename,
    period_bounds,
    source_url_for,
)


SAMPLE = {
    "business_unit": "1115",
    "agency_name": "Bus., Consumer Srvcs & Housing",
    "department_name": "Department of Cannabis Control",
    "document_id": "1115.00021375.0.00001.0002",
    "related_document": "",
    "accounting_date": "2026-05-29",
    "fiscal_year_begin": "2025",
    "accounting_period": "11",
    "VENDOR_NAME": "CARDINAL INV PROP GODDARD LP",
    "account": "5324450",
    "account_type": "Operating Expense & Equipment",
    "account_category": "Facilities Operation",
    "account_sub_category": "Facilities Operation",
    "account_description": "Rent -Bldgs&Grounds(Non State)",
    "fund_code": "3335",
    "fund_group": "Special Funds",
    "fund_description": "Cannabis Tax Cannabis Control",
    "program_code": "1460010000",
    "program_description": "Cannabis Control - Support",
    "sub_program_description": "Department of Cannabis Control",
    "budget_reference": "501",
    "budget_reference_category": "State Operations",
    "budget_reference_sub_category": "Non-Budget Act",
    "budget_reference_description": "Non-BA State Operations-Sup501",
    "year_of_enactment": "2025",
    "monetary_amount": "17152.000",
}


def test_ca_fy_rolls_on_july_1():
    assert california_fiscal_year(date(2025, 7, 1)) == 2025
    assert california_fiscal_year(date(2026, 6, 30)) == 2025
    assert california_fiscal_year(date(2026, 7, 1)) == 2026


def test_ca_fy_bounds_are_july_through_june():
    start, end = fiscal_year_bounds(2025)
    assert start.isoformat() == "2025-07-01"
    assert end.isoformat() == "2026-06-30"


def test_parse_vendor_filename():
    assert parse_vendor_filename("Vendor_FY25P11.csv") == (2025, 11)
    assert parse_vendor_filename("Vendor_FY16P01.csv") == (2016, 1)
    assert parse_vendor_filename("Spending_FY25P11.csv") is None


def test_period_11_is_may_of_next_calendar_year():
    start, end = period_bounds(2025, 11)
    assert start.isoformat() == "2026-05-01"
    assert end.isoformat() == "2026-05-31"


def test_files_for_window_keeps_overlapping_months_newest_first():
    pointer = [
        {
            "FileName": "Vendor_FY25P10.csv",
            "Download": "https://example.test/Vendor_FY25P10.csv",
        },
        {
            "FileName": "Vendor_FY25P11.csv",
            "Download": "https://example.test/Vendor_FY25P11.csv",
        },
        {
            "FileName": "Vendor_FY24P12.csv",
            "Download": "https://example.test/Vendor_FY24P12.csv",
        },
    ]
    files = files_for_window(
        pointer, fiscal_year=2025, since=date(2026, 5, 16), until=date(2026, 8, 14)
    )
    assert [f["filename"] for f in files] == ["Vendor_FY25P11.csv"]


def test_map_row_does_not_invent_award_type_or_amount_type():
    rec = map_row(SAMPLE, file_url="https://example.test/Vendor_FY25P11.csv")
    assert rec.get("_review") is None
    assert "award_type" not in rec
    assert "amount_type" not in rec
    assert rec["jurisdiction"] == "state"
    assert rec["state_code"] == "CA"
    assert rec["recipient_name"] == "CARDINAL INV PROP GODDARD LP"
    assert rec["recipient_state"] is None
    assert rec["awarding_agency"] == "Department of Cannabis Control"
    assert rec["amount"] == 17152.0
    assert rec["period_of_performance_start"] == "2026-05-29"
    assert rec["period_of_performance_end"] is None
    assert rec["fiscal_year"] == 2025
    assert rec["award_id"].startswith("CA-")
    assert "1115.00021375.0.00001.0002" in rec["award_id"]
    assert rec["source_url"] == source_url_for(
        document_id="1115.00021375.0.00001.0002",
        account="5324450",
        accounting_date="2026-05-29",
        program_code="1460010000",
    )
    assert rec["source_url"].startswith("https://open.fiscal.ca.gov/")
    assert "Cannabis Control - Support" in rec["description"]
    assert rec["program"] == "Cannabis Control - Support"


def test_confidential_vendor_goes_to_review():
    row = dict(SAMPLE)
    row["VENDOR_NAME"] = "CONFIDENTIAL"
    rec = map_row(row)
    assert rec["_review"] is True
    assert rec["reason"] == "unexpected_structure"
    assert "Confidential" in rec["notes"]
    assert "amount" not in rec


def test_negative_amount_goes_to_review():
    row = dict(SAMPLE)
    row["VENDOR_NAME"] = "YVES VOLTAIRE"
    row["monetary_amount"] = "-19212.470"
    rec = map_row(row)
    assert rec["_review"] is True
    assert "amount" not in rec
    assert "-19212.47" in rec["notes"]


def test_grant_account_category_is_not_mapped_to_grant():
    row = dict(SAMPLE)
    row["account_category"] = "Grants and Subventions"
    row["account_description"] = "Grants & Subventions - Non-Gov"
    rec = map_row(row)
    assert rec.get("_review") is None
    assert "award_type" not in rec


def test_missing_document_id_goes_to_review():
    rec = map_row({"VENDOR_NAME": "ACME", "monetary_amount": "1.00"})
    assert rec["_review"] is True


def test_iso_date_accepts_date_only():
    assert iso_date("2026-05-01") == "2026-05-01"
    assert iso_date("2026-05-01 00:00:00") == "2026-05-01"
    assert iso_date("05/01/2026") is None
