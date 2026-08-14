"""Offline tests for PRAC fraud-case mapping and allowlist."""
from ingest.prac import (
    load_allowlist,
    map_defendant_type,
    map_program,
    map_status,
    parse_exact_amount,
    parse_listing,
    source_allowed,
    to_record,
)

LISTING = """
<div class="views-row">
  <div class="display__condensed--superscript"><span>Department of Justice</span></div>
  <h2 class="display__condensed--title">
    <a href="https://www.justice.gov/usao-ndtx/pr/dallas-based-bus-companies-settle-ppp-lawsuit-39-million">
      Dallas-Based Bus Companies Settle PPP Lawsuit for $3.9 Million
    </a>
  </h2>
  <div class="display__condensed--body">
    <div class="field field--name-body">
      Tornado Bus Company, Inc. agreed to pay $3.9 million to resolve allegations
      involving the Small Business Administration’s Paycheck Protection Program (PPP).
    </div>
  </div>
  <div class="display__condensed--footer">
    <span>Investigative Results | <time datetime="2026-08-12T12:00:00Z">8/12/2026</time></span>
  </div>
</div>
<div class="views-row">
  <h2 class="display__condensed--title">
    <a href="https://www.thecentersquare.com/national/article_not-trusted.html">
      Four men sentenced in PPP case
    </a>
  </h2>
  <div class="field field--name-body">Paycheck Protection Program fraud.</div>
  <time datetime="2026-08-11T12:00:00Z">8/11/2026</time>
</div>
<div class="views-row">
  <h2 class="display__condensed--title">
    <a href="https://www.justice.gov/usao-ndga/pr/griffin-grifter-sentenced-federal-prison-dogfighting">
      Griffin Grifter Sentenced to Federal Prison for Dogfighting
    </a>
  </h2>
  <div class="field field--name-body">A man was sentenced for dogfighting.</div>
  <time datetime="2026-08-10T12:00:00Z">8/10/2026</time>
</div>
"""


def test_allowlist_accepts_doj_and_prac_rejects_news():
    domains = load_allowlist()
    assert source_allowed("https://www.justice.gov/usao-ma/pr/example", domains)
    assert source_allowed("https://www.pandemicoversight.gov/news/articles/x", domains)
    assert source_allowed("https://www.courtlistener.com/docket/1/", domains)
    assert not source_allowed("https://www.thecentersquare.com/national/x", domains)
    assert not source_allowed("https://www.meritalk.com/articles/x", domains)


def test_hedged_amount_is_not_stored():
    assert parse_exact_amount("sentenced for fraudulently obtaining over $40,000") is None
    assert parse_exact_amount("stealing more than $1 million") is None
    assert parse_exact_amount("agree to pay over $2.3 million") is None
    assert parse_exact_amount("Settle PPP Lawsuit for $3.9 Million") == 3_900_000.0
    assert parse_exact_amount("Sentenced for $17 Million Pandemic-Relief Fraud") == 17_000_000.0


def test_program_and_status_and_defendant():
    assert map_program("Paycheck Protection Program loans") == "Paycheck Protection Program (PPP)"
    assert map_status("companies settle PPP lawsuit") == "settled"
    assert map_status("woman sentenced to two years") == "convicted"
    assert map_status("pleads guilty to stealing") == "convicted"
    assert map_status("was indicted on Friday") == "charged"
    assert map_defendant_type("Tornado Bus Company, Inc. and El Expreso Group, LLC") == "company"
    assert map_defendant_type("Four Georgia Men Sentenced") == "multiple"
    assert map_defendant_type("Kapolei Woman Sentenced") == "individual"


def test_listing_and_to_record_allowlist():
    rows = parse_listing(LISTING)
    assert len(rows) == 3
    allow = load_allowlist()
    ok = to_record(rows[0], allow)
    assert ok.get("_review") is None
    assert ok["agency_program"].startswith("Paycheck Protection")
    assert ok["status"] == "settled"
    assert ok["defendant_type"] == "company"
    assert ok["amount_alleged"] == 3_900_000.0
    assert ok["source_url"].startswith("https://www.justice.gov/")
    assert ok["filing_date"] == "2026-08-12"

    blocked = to_record(rows[1], allow)
    assert blocked["_review"] is True
    assert "allowlist" in blocked["notes"]

    no_program = to_record(rows[2], allow)
    assert no_program["_review"] is True
    assert "agency_program" in no_program["notes"]
