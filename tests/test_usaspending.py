"""Offline tests for USAspending mapping and agency allowlist."""
from ingest.usaspending import (
    fiscal_year_bounds,
    federal_fiscal_year,
    iso_date,
    load_agency_catalog,
    map_award,
    recipient_state,
    resolve_agencies,
    source_url_for,
)


def test_fiscal_year_bounds():
    start, end = fiscal_year_bounds(2026)
    assert start.isoformat() == "2025-10-01"
    assert end.isoformat() == "2026-09-30"


def test_federal_fy_october_rolls_forward():
    from datetime import date

    assert federal_fiscal_year(date(2025, 10, 1)) == 2026
    assert federal_fiscal_year(date(2026, 9, 30)) == 2026


def test_default_allowlist_is_five_agencies():
    catalog = load_agency_catalog()
    agencies = resolve_agencies(catalog)
    assert [a["abbreviation"] for a in agencies] == ["HHS", "DOL", "SBA", "EPA", "DOT"]
    assert all(a["name"].startswith("Department") or a["abbreviation"] in ("SBA", "EPA") for a in agencies)


def test_agency_flag_accepts_abbrev_or_name():
    catalog = load_agency_catalog()
    by_abbr = resolve_agencies(catalog, agency_flag="EPA")
    by_name = resolve_agencies(catalog, agency_flag="Environmental Protection Agency")
    assert by_abbr[0]["abbreviation"] == "EPA"
    assert by_name[0]["name"] == "Environmental Protection Agency"


def test_map_contract_uses_award_amount_as_obligated():
    row = {
        "Award ID": "EPW17009",
        "Recipient Name": "ABT GLOBAL LLC",
        "Recipient Location": {"state_code": "MA", "country_name": "UNITED STATES"},
        "Start Date": "2017-02-11",
        "End Date": "2023-03-10",
        "Award Amount": 7655172.98,
        "Awarding Agency": "Environmental Protection Agency",
        "Description": "NCEE mission support",
        "generated_internal_id": "CONT_AWD_EPW17009_6800_-NONE-_-NONE-",
    }
    rec = map_award(row, "contract", 2026)
    assert rec.get("_review") is None
    assert rec["award_id"] == "CONT_AWD_EPW17009_6800_-NONE-_-NONE-"
    assert rec["amount"] == 7655172.98
    assert rec["amount_type"] == "obligated"
    assert rec["award_type"] == "contract"
    assert rec["recipient_state"] == "MA"
    assert rec["period_of_performance_start"] == "2017-02-11"
    assert rec["period_of_performance_end"] == "2023-03-10"
    assert rec["fiscal_year"] == 2026
    assert rec["source_url"] == source_url_for(rec["award_id"])
    assert rec["source_url"].startswith("https://www.usaspending.gov/award/")


def test_loan_is_not_posted_as_obligated_amount():
    row = {
        "Award ID": "9991109004",
        "Recipient Name": "SANDO'S WHOLE LIFE CENTER FOR HEALTH LLC",
        "Recipient Location": {"state_code": "PA"},
        "Issued Date": "2025-10-27",
        "Loan Value": 0.0,
        "Subsidy Cost": 0.0,
        "Awarding Agency": "Small Business Administration",
        "Description": "TO AID SMALL BUSINESSES",
        "generated_internal_id": "ASST_NON_9991109004_073",
    }
    rec = map_award(row, "loan", 2026)
    assert rec["_review"] is True
    assert "amount" not in rec
    assert "Loan Value" in rec["notes"] or "loan_value" in rec["notes"]
    assert rec["source_url"] == source_url_for("ASST_NON_9991109004_073")


def test_missing_generated_id_goes_to_review():
    rec = map_award({"Award Amount": 1, "Start Date": "2026-01-01"}, "grant", 2026)
    assert rec["_review"] is True


def test_iso_date_strips_time():
    assert iso_date("2026-07-24 13:48:05") == "2026-07-24"
    assert iso_date("not-a-date") is None


def test_recipient_state_requires_usps():
    assert recipient_state({"Recipient Location": {"state_code": "oh"}}) == "OH"
    assert recipient_state({"Recipient Location": {"state_code": "Ohio"}}) is None
