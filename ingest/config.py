"""Environment configuration. Loads from .env locally, from real env vars
(GitHub Actions secrets) in CI."""
import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL")
CONGRESS_GOV_API_KEY = os.environ.get("CONGRESS_GOV_API_KEY")

# Verified 2026-08 against disclosures-clerk.house.gov/FinancialDisclosure:
# annual bulk ZIPs are linked as /public_disc/financial-pdfs/{year}FD.zip
HOUSE_BULK_ZIP_URL_TEMPLATE = (
    "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
)
# PTRs live under ptr-pdfs/; annual / candidate / other FD under financial-pdfs/.
HOUSE_PTR_PDF_URL_TEMPLATE = (
    "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc_id}.pdf"
)
HOUSE_FD_PDF_URL_TEMPLATE = (
    "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}/{doc_id}.pdf"
)

# House bulk index FilingType letter codes (from 2025FD.xml live sample).
# This pass only ingests PTRs (P). Others noted for a later annual-holdings phase.
HOUSE_FILING_TYPE_LABELS = {
    "P": "PTR",
    "A": "Amendment",
    "B": "Blind Trust",
    "C": "Candidate",
    "D": "Candidate Termination",  # provisional
    "E": "Extension",
    "G": "Gift Waiver",  # provisional
    "H": "Honoraria",  # provisional
    "O": "Annual Report",
    "T": "Termination",
    "W": "Withdrawal",  # provisional
    "X": "Annual Report (amended/other)",  # common annual-like filings
}

SENATE_EFD_BASE_URL = "https://efdsearch.senate.gov"
SENATE_EFD_SEARCH_URL = f"{SENATE_EFD_BASE_URL}/search/"
SENATE_EFD_SEARCH_HOME_URL = f"{SENATE_EFD_BASE_URL}/search/home/"
SENATE_EFD_REPORT_DATA_URL = f"{SENATE_EFD_BASE_URL}/search/report/data/"

# Live form checkbox values on efdsearch.senate.gov/search/ (verified 2026-08):
#   7  = Annual
#  11  = Periodic Transactions
#  10  = Due Date Extension
#  14  = Blind Trusts
#  15  = Other Documents
SENATE_REPORT_TYPE_PTR = "11"
SENATE_REPORT_TYPE_ANNUAL = "7"

# Filer type checkboxes:
#   1 = Senator, 4 = Candidate, 5 = Former Senator
SENATE_FILER_TYPE_SENATOR = "1"

CONGRESS_GOV_API_BASE = "https://api.congress.gov/v3"

REQUEST_TIMEOUT_SECONDS = 60
REQUEST_DELAY_SECONDS = 1.5  # politeness delay between requests to gov sites

USER_AGENT = (
    "rollcalltrack.com research bot "
    "(financial-disclosure ingest; contact: hello@rollcalltrack.com)"
)


def require_config(dry_run: bool = False, source: str = "all"):
    """Call at startup — fail loudly if required config is missing.

    Dry-run parse-only paths only need a DB URL when we still open a
    connection for member matching; house/senate dry-run skips the DB
    entirely. Congress.gov enrichment always needs its API key.
    """
    missing = []
    # Dry-run of house/senate alone needs no secrets — pure live parse check.
    if not dry_run and not DATABASE_URL:
        missing.append("DATABASE_URL")
    if source in ("congress_enrich", "all") and not dry_run and not CONGRESS_GOV_API_KEY:
        missing.append("CONGRESS_GOV_API_KEY")
    if source == "congress_enrich" and dry_run and not CONGRESS_GOV_API_KEY:
        # enrich dry-run still hits the API
        missing.append("CONGRESS_GOV_API_KEY")
    if missing:
        raise RuntimeError(
            f"Missing required environment variables: {', '.join(missing)}"
        )
