from ingest.doj_fraud import (
    parse_hcf_summaries,
    parse_rss_items,
    slug_id,
    to_case,
)

HCF = """
<main>
  <h3>U.S. v. Shamso Ahmed Hassan, et al.</h3>
  <p>Shamso Ahmed Hassan, 55, of Brooklyn Park, Minnesota, and Hanaan Mursal Yusuf,
  25, of Brooklyn Park, Minnesota, were charged by indictment with conspiracy to
  commit health care fraud in a Medicaid autism-services scheme.</p>
  <h3>U.S. v. Fahima Mahamud</h3>
  <p>Fahima Mahamud, 50, of Minneapolis, Minnesota, was charged by Information
  with wire fraud after submitting inflated claims to Feeding Our Future for
  meals at a child nutrition site.</p>
</main>
"""

RSS = """
<rss><channel>
  <item>
    <title>Minnesota Health Care Fraud Takedown Results in Charges Against 15 Defendants for Over $90M in Fraud</title>
    <link>https://www.justice.gov/opa/pr/minnesota-health-care-fraud-takedown</link>
    <pubDate>Thu, 21 May 2026 12:00:00 +0000</pubDate>
  </item>
  <item>
    <title>ENRD Statement After Return from APEC Forestry Ministerial in China</title>
    <link>https://www.justice.gov/opa/pr/trees</link>
    <pubDate>Fri, 14 Aug 2026 12:00:00 +0000</pubDate>
  </item>
</channel></rss>
"""


def test_slug_id_is_stable():
    assert slug_id("U.S. v. Fahima Mahamud") == slug_id("U.S. v. Fahima Mahamud")
    assert slug_id("U.S. v. Fahima Mahamud") != slug_id("U.S. v. Other")


def test_parse_hcf_summaries():
    rows = parse_hcf_summaries(
        HCF,
        page_url="https://www.justice.gov/criminal/x",
        announcement_date="2026-05-21",
    )
    assert len(rows) == 2
    first = to_case(rows[0])
    assert first.get("_review") is None
    assert first["agency_program"] == "Medicaid"
    assert first["status"] == "charged"
    assert first["defendant_type"] == "multiple"
    assert first["filing_date"] == "2026-05-21"
    assert first["source_url"].endswith("#u-s-v-shamso-ahmed-hassan-et-al")

    second = to_case(rows[1])
    assert second["agency_program"] == "Federal Child Nutrition Program"
    assert second["defendant_type"] == "individual"


def test_rss_keeps_fraud_drops_unrelated():
    items = parse_rss_items(RSS, days_back=400)
    titles = [i["case_title"] for i in items]
    assert any("Medicaid" in t or "Health Care Fraud" in t for t in titles)
    assert all("Forestry" not in t for t in titles)
