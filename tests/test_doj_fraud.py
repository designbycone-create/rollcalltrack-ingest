from ingest.doj_fraud import (
    parse_hcf_summaries,
    parse_national_hcf_summaries,
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
    assert first["agency"] == "Department of Health and Human Services"
    assert first["state_code"] == "MN"
    assert first["status"] == "charged"
    assert first["defendant_type"] == "multiple"
    assert first["filing_date"] == "2026-05-21"
    assert first["source_url"].endswith("#u-s-v-shamso-ahmed-hassan-et-al")

    second = to_case(rows[1])
    assert second["agency_program"] == "Federal Child Nutrition Program"
    assert second["agency"] == "Department of Agriculture"
    assert second["state_code"] == "MN"
    assert second["defendant_type"] == "individual"


NATIONAL_HCF = """
<main>
  <p><em><strong>Middle District of Alabama</strong></em></p>
  <ul>
    <li><strong>New Life Center for Change, Inc</strong>.
    <strong>d/b/a Teen University</strong>, located in Phenix City, Alabama,
    and its owner, <strong>Alfonza Smith</strong>, reached a civil settlement
    to pay $300,000.00 to resolve allegations that the company billed the
    Alabama Medicaid Agency for services that never took place.</li>
  </ul>
  <p><em><strong>District of Arizona</strong></em></p>
  <ul>
    <li><strong>Susie Kamien</strong>, 55, of West Chester, Illinois, was
    charged by information with conspiracy to commit health care fraud in
    connection with submitting false claims to Medicare.</li>
    <li><strong>Jimmy Muyumbu</strong>, 38, formerly of Glendale, Arizona,
    was charged by indictment with health care fraud in connection with an
    alleged scheme that billed Arizona Medicaid over $44 million.</li>
  </ul>
  <p><em><strong>District of Hawaii</strong></em></p>
  <ul>
    <li><strong>Henry Quan</strong>, 54, of Honolulu, Hawaii, was charged by
    complaint with health care fraud in connection with a scheme to bill
    Medicare for prescription drugs that were never dispensed.</li>
  </ul>
</main>
"""


def test_parse_national_hcf_covers_multiple_states():
    rows = parse_national_hcf_summaries(
        NATIONAL_HCF,
        page_url="https://www.justice.gov/criminal/x-national",
        announcement_date="2026-06-23",
    )
    assert len(rows) == 4
    by_title = {r["case_title"]: r for r in rows}
    assert by_title["New Life Center for Change, Inc, et al."]["venue_state"] == "AL"
    assert by_title["Susie Kamien"]["venue_state"] == "AZ"
    assert by_title["Henry Quan"]["venue_state"] == "HI"

    alabama = to_case(rows[0])
    assert alabama.get("_review") is None
    assert alabama["state_code"] == "AL"
    assert alabama["agency_program"] == "Medicaid"
    assert alabama["agency"] == "Department of Health and Human Services"
    assert alabama["status"] == "settled"
    assert alabama["filing_date"] == "2026-06-23"

    arizona = to_case(by_title["Susie Kamien"])
    assert arizona["state_code"] == "AZ"
    assert arizona["agency_program"] == "Medicare"
    assert arizona["defendant_type"] == "individual"


NATIONAL_HCF_H4 = """
<main>
  <h4><strong>District of Arizona</strong></h4>
  <ul>
    <li><strong>Farrukh Jarar Ali</strong>, 41, of Pakistan, was charged by
    indictment with conspiracy to commit health care fraud in connection
    with substance abuse clinics that billed Arizona Medicaid.</li>
  </ul>
  <h4><strong>District of Columbia</strong></h4>
  <ul>
    <li><strong>Jane Roe</strong>, of Washington, D.C., was charged by
    information with health care fraud for false claims to Medicare.</li>
  </ul>
</main>
"""


def test_parse_national_hcf_h4_district_headers():
    rows = parse_national_hcf_summaries(
        NATIONAL_HCF_H4,
        page_url="https://www.justice.gov/criminal/x-2025",
        announcement_date="2025-06-30",
    )
    assert len(rows) == 2
    assert rows[0]["venue_state"] == "AZ"
    assert rows[1]["venue_state"] == "DC"
    rec = to_case(rows[0])
    assert rec.get("_review") is None
    assert rec["state_code"] == "AZ"
    assert rec["agency_program"] == "Medicaid"


def test_rss_keeps_fraud_drops_unrelated():
    items = parse_rss_items(RSS, days_back=400)
    titles = [i["case_title"] for i in items]
    assert any("Medicaid" in t or "Health Care Fraud" in t for t in titles)
    assert all("Forestry" not in t for t in titles)
