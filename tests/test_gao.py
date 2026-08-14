"""Offline tests for GAO / PaymentAccuracy improper-payment mapping."""
from ingest.gao import (
    discover_program_urls,
    parse_money_to_dollars,
    parse_program_page,
    records_from_parse,
    require_report,
    load_report_catalog,
)

FIXTURE = """
<html><body>
<p class="breadcrumbs"><a href="/">Home</a> > Medicaid</p>
<h3>Sponsoring agency:  Department of Health and Human Services</h3>
<h1>Centers for Medicare &amp; Medicaid Services (CMS) - Medicaid</h1>
<div class="metrics-summary" data-year="2025">
  <h2>$610,988 M</h2>
  <p>in <strong>FY 2025 outlays</strong></p>
</div>
<div class="grid-row" data-year="2025">
  <h2>FY 2025 improper payment estimates</h2>
  <p>Amount of overpayments within the agency's control</p>
  <p>$0.0 M</p>
  <p>Amount of overpayments outside the agency's control</p>
  <p>$35,029.54 M</p>
  <p>Amount of underpayments</p>
  <p>$256.75 M</p>
  <p>The amount of improper payments that were paid to the right recipient
     for the correct amount but were considered technically improper because
     of failure to follow statute or regulation</p>
  <p>$2,100.5 M</p>
  <p>The amount that could either be proper or improper but the agency is
     unable to determine whether it was proper or improper as a result of
     insufficient or lack of documentation</p>
  <p>$0.0 M</p>
</div>
</body></html>
"""

REPORT = {
    "report_id": "GAO-26-108694",
    "title": "Payment Integrity: Agencies' Estimated Improper Payments Increased to $186 Billion in Fiscal Year 2025",
    "url": "https://www.gao.gov/products/gao-26-108694",
    "report_date": "2026-04-27",
}


def test_parse_money_millions_and_billions():
    assert parse_money_to_dollars("$35,029.54 M") == 35_029_540_000.0
    assert parse_money_to_dollars("$186 billion") == 186_000_000_000.0
    assert parse_money_to_dollars("not money") is None


def test_catalog_has_fy2025_gao_report():
    rec = require_report(load_report_catalog(), 2025)
    assert rec["report_id"] == "GAO-26-108694"
    assert rec["url"].startswith("https://www.gao.gov/")


def test_parse_program_page_uses_published_cause_dollars():
    parsed = parse_program_page(FIXTURE, 2025, "https://paymentaccuracy.gov/program/medicaid")
    assert parsed["agency"] == "Department of Health and Human Services"
    assert "Medicaid" in parsed["program_name"]
    assert not parsed["program_name"].startswith(">")
    assert parsed["error"] is None
    # 35029.54M + 0.0M overpayments
    assert parsed["causes"]["overpayment"] == 35_029_540_000.0
    assert parsed["causes"]["underpayment"] == 256_750_000.0
    assert parsed["causes"]["documentation_error"] == 2_100_500_000.0
    assert "fraud" not in parsed["causes"]


def test_records_set_low_and_high_to_same_published_figure():
    parsed = parse_program_page(FIXTURE, 2025, "https://example.test/medicaid")
    recs = records_from_parse(parsed, 2025, REPORT)
    over = next(r for r in recs if r["cause_category"] == "overpayment")
    assert over["estimated_amount_low"] == over["estimated_amount_high"] == 35_029_540_000.0
    assert over["source_url"] == REPORT["url"]
    assert over["fiscal_year"] == 2025
    assert over["report_date"] == "2026-04-27"


def test_does_not_multiply_outlays_by_rate():
    parsed = parse_program_page(FIXTURE, 2025, "https://example.test/medicaid")
    # 610988M * anything would not equal 35029.54M
    assert parsed["causes"]["overpayment"] != 610_988_000_000.0


def test_missing_year_goes_to_review():
    parsed = parse_program_page(FIXTURE, 2019, "https://example.test/medicaid")
    recs = records_from_parse(parsed, 2019, REPORT)
    assert recs[0]["_review"] is True


def test_discover_program_urls_dedupes():
    html = """
    <a href="/program/hhs-medicaid">Medicaid</a>
    <a href="/program/hhs-medicaid">Medicaid again</a>
    <a href="https://paymentaccuracy.gov/program/ssa-ssi">SSI</a>
    """
    urls = discover_program_urls(html)
    assert len(urls) == 2
    assert urls[0].endswith("/program/hhs-medicaid")
