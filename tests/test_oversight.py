"""Offline tests for Oversight.gov IG report mapping."""
from ingest.oversight import (
    financial_impact_from_fields,
    map_report_type,
    parse_detail,
    parse_listing,
    parse_money,
    summarize,
    to_record,
)

LISTING = """
<table class="listing-table">
<tbody>
<tr class="listing-table__row">
  <td data-label="Report Date"><time datetime="2026-08-13T12:00:00Z">08/13/2026</time></td>
  <td data-label="Agency Reviewed / Investigated">General Services Administration</td>
  <td data-label="Report Title">Catalog Data Quality</td>
  <td data-label="Type">Audit</td>
  <td data-label="Location">Agency-Wide</td>
  <td data-label="action-cell-report-link"><a href="/reports/audit/catalog-data">View Report</a></td>
</tr>
<tr class="listing-table__row">
  <td data-label="Report Date"><time datetime="2026-08-12T12:00:00Z">08/12/2026</time></td>
  <td data-label="Agency Reviewed / Investigated">Department of War</td>
  <td data-label="Report Title">Operation Atlantic Resolve</td>
  <td data-label="Type">Other</td>
  <td data-label="Location">Agency-Wide</td>
  <td data-label="action-cell-report-link"><a href="/reports/other/oar">View Report</a></td>
</tr>
</tbody>
</table>
"""

DETAIL = """
<html><body>
<h1>Catalog Data Quality</h1>
<div class="field field--name-field-report-agency-reviewed">
  Agencies Reviewed/Investigated General Services Administration
</div>
<div class="field field--name-field-report-type">Report Type Audit</div>
<div class="field field--name-field-report-date-issued">
  Date Issued <time datetime="2026-08-13T12:00:00Z">Thursday, August 13, 2026</time>
</div>
<div class="field field--name-field-report-number-of-recs">Number of Recommendations 6</div>
<div class="field field--name-field-net-questioned-costs">Questioned Costs $0</div>
<div class="field field--name-field-net-funds-for-better-use">Funds for Better Use $0</div>
<div class="field field--name-body">
  <p>FAS catalog data contains inconsistent manufacturer part numbers.</p>
  <p>Identical products cannot be compared reliably across systems.</p>
  <p>The OIG issued recommendations to the FAS Commissioner.</p>
  <p>A fourth sentence should not appear in the summary.</p>
</div>
</body></html>
"""


def test_type_map_known_and_unknown():
    assert map_report_type("Audit") == "audit"
    assert map_report_type("Inspection / Evaluation") == "evaluation"
    assert map_report_type("Semiannual Report") == "semiannual_report"
    assert map_report_type("Other") is None
    assert map_report_type("Review") is None


def test_financial_impact_does_not_sum_different_fields():
    assert financial_impact_from_fields(0.0, 0.0) == 0.0
    assert financial_impact_from_fields(5_000_000.0, 5_000_000.0) == 5_000_000.0
    assert financial_impact_from_fields(5_000_000.0, 1_000.0) is None
    assert financial_impact_from_fields(None, None) is None
    assert financial_impact_from_fields(12.0, None) == 12.0


def test_parse_listing_rows():
    rows = parse_listing(LISTING)
    assert len(rows) == 2
    assert rows[0]["agency"] == "General Services Administration"
    assert rows[0]["publish_date"] == "2026-08-13"
    assert rows[0]["source_url"].endswith("/reports/audit/catalog-data")
    assert rows[1]["report_type_raw"] == "Other"


def test_parse_detail_and_summary():
    d = parse_detail(DETAIL)
    assert d["agency"] == "General Services Administration"
    assert d["recommendations_count"] == 6
    assert d["financial_impact"] == 0.0
    assert d["publish_date"] == "2026-08-13"
    assert "fourth sentence" not in d["summary"].lower()
    assert d["summary"].startswith("FAS catalog data")
    assert d["summary"].count(".") >= 2


def test_other_type_goes_to_review():
    listing = parse_listing(LISTING)[1]
    rec = to_record(listing, None)
    assert rec["_review"] is True
    assert "Other" in rec["notes"]


def test_audit_maps_without_inventing_impact():
    listing = parse_listing(LISTING)[0]
    rec = to_record(listing, parse_detail(DETAIL))
    assert rec.get("_review") is None
    assert rec["report_type"] == "audit"
    assert rec["financial_impact"] == 0.0
    assert rec["source_url"].startswith("https://www.oversight.gov/")


def test_parse_money_requires_dollar_sign():
    assert parse_money("Questioned Costs $1,234.50") == 1234.50
    assert parse_money("six million dollars") is None


def test_summarize_caps_at_three_sentences():
    text = "One found X. Two found Y. Three found Z. Four should drop."
    s = summarize(text)
    assert "Four" not in s
    assert s.startswith("One found X.")
