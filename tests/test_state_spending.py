"""Offline tests for the 50-state spending catalog and mapper."""
from ingest.legiscan import STATE_CODES
from ingest.state_spending import (
    CATALOG_PATH,
    load_catalog,
    map_generic_row,
    resolve_states,
    run,
)


def test_catalog_covers_exactly_50_states():
    catalog = load_catalog()
    assert set(catalog["states"]) == set(STATE_CODES)
    assert CATALOG_PATH.is_file()


def test_every_state_has_portal_and_adapter():
    catalog = load_catalog()
    for code, spec in catalog["states"].items():
        assert spec.get("name"), code
        assert spec.get("portal_url", "").startswith("http"), code
        assert spec.get("adapter") in ("open_fiscal", "socrata", "csv", "ckan", "unsupported"), code
        assert "is_expenditures_not_awards" in spec, code
        if spec["adapter"] == "socrata":
            assert spec.get("soda_domain")
            assert spec.get("soda_dataset")
            assert spec.get("map", {}).get("vendor")
            assert spec.get("map", {}).get("amount")
        if spec["adapter"] == "csv":
            assert spec.get("csv_url")
            assert spec.get("map", {}).get("vendor")
        if spec["adapter"] == "ckan":
            assert spec.get("ckan_package_show")
            assert spec.get("map", {}).get("vendor")


def test_california_is_the_open_fiscal_adapter():
    spec = load_catalog()["states"]["CA"]
    assert spec["adapter"] == "open_fiscal"
    assert "open.fiscal.ca.gov" in spec["portal_url"]
    assert spec["is_expenditures_not_awards"] is True


def test_map_generic_row_omits_award_enums():
    spec = {
        "map": {
            "vendor": "payee",
            "amount": "amount",
            "agency": "dept",
            "date": "paid_date",
            "program": "program",
            "id": "doc",
        },
        "portal_url": "https://example.test/checkbook",
        "is_expenditures_not_awards": True,
    }
    rec = map_generic_row(
        {"payee": "ACME LLC", "amount": "12.50", "dept": "DOT", "paid_date": "2026-05-01", "program": "Roads", "doc": "A1"},
        spec,
        state_code="NY",
        fiscal_year=2025,
    )
    assert rec.get("_review") is None
    assert "award_type" not in rec
    assert "amount_type" not in rec
    assert rec["jurisdiction"] == "state"
    assert rec["state_code"] == "NY"
    assert rec["amount"] == 12.5
    assert rec["recipient_name"] == "ACME LLC"
    assert rec["source_url"].startswith("https://example.test/checkbook")
    assert rec["fiscal_year"] == 2025


def test_generic_confidential_and_negative_go_to_review():
    spec = {
        "map": {"vendor": "v", "amount": "a", "agency": "g", "date": "d", "id": "i"},
        "portal_url": "https://example.test/",
    }
    assert map_generic_row({"v": "Confidential", "a": "10", "g": "X", "d": "2026-01-01", "i": "1"}, spec, "TX", 2025)["_review"]
    assert map_generic_row({"v": "Undisclosed", "a": "10", "g": "X", "d": "2026-01-01", "i": "1"}, spec, "CT", 2025)["_review"]
    assert map_generic_row({"v": "ACME", "a": "-10", "g": "X", "d": "2026-01-01", "i": "1"}, spec, "TX", 2025)["_review"]


def test_resolve_states_all_or_one():
    assert resolve_states("de", False) == ["DE"]
    assert len(resolve_states(None, True)) == 50


def test_unsupported_state_is_skipped_not_posted():
    result = run(states=["WY"], fiscal_year=2025, dry_run=True, max_records=1)
    assert result["states"]["WY"]["status"] in ("skipped", "unsupported")
    assert result["posted"] == 0
    assert result["mapped"] == 0


def test_mdy_dates_parse():
    spec = {
        "map": {"vendor": "v", "amount": "a", "agency": "g", "date": "d", "id": "i"},
        "portal_url": "https://example.test/",
    }
    rec = map_generic_row(
        {"v": "ACME", "a": "10", "g": "DOT", "d": "07/15/2026", "i": "1"}, spec, "OK", 2026
    )
    assert rec.get("_review") is None
    assert rec["period_of_performance_start"] == "2026-07-15"


def test_fiscal_year_only_becomes_july_1():
    spec = {
        "map": {"vendor": "v", "amount": "a", "agency": "g", "date": "fy", "id": "i"},
        "portal_url": "https://example.test/",
    }
    rec = map_generic_row(
        {"v": "ACME", "a": "10", "g": "DOT", "fy": "2026", "i": "1"}, spec, "MO", 2026
    )
    assert rec.get("_review") is None
    assert rec["period_of_performance_start"] == "2026-07-01"
