"""Environment configuration. Loads from .env locally, from real env vars
(GitHub Actions secrets) in CI.

Horizons backend is PocketBase (confirmed live):
  base URL path: {host}/hcgi/platform
  e.g. https://<app-id>.app-preview.com/hcgi/platform
"""
import os
from dotenv import load_dotenv

load_dotenv()

# Accept either name. DATABASE_URL historically meant Postgres DSN; on
# Horizons it is the PocketBase base URL (http/https).
DATABASE_URL = os.environ.get("DATABASE_URL") or os.environ.get("POCKETBASE_URL")
POCKETBASE_URL = DATABASE_URL
POCKETBASE_ADMIN_EMAIL = os.environ.get("POCKETBASE_ADMIN_EMAIL")
POCKETBASE_ADMIN_PASSWORD = os.environ.get("POCKETBASE_ADMIN_PASSWORD")

CONGRESS_GOV_API_KEY = os.environ.get("CONGRESS_GOV_API_KEY")

# Verified 2026-08 against disclosures-clerk.house.gov/FinancialDisclosure
HOUSE_BULK_ZIP_URL_TEMPLATE = (
    "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
)
HOUSE_PTR_PDF_URL_TEMPLATE = (
    "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc_id}.pdf"
)
HOUSE_FD_PDF_URL_TEMPLATE = (
    "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}/{doc_id}.pdf"
)

HOUSE_FILING_TYPE_LABELS = {
    "P": "PTR",
    "A": "Amendment",
    "B": "Blind Trust",
    "C": "Candidate",
    "D": "Candidate Termination",
    "E": "Extension",
    "G": "Gift Waiver",
    "H": "Honoraria",
    "O": "Annual Report",
    "T": "Termination",
    "W": "Withdrawal",
    "X": "Annual Report (amended/other)",
}

# Map Clerk codes / free text → PB disclosure_filings.filing_type select values
HOUSE_TO_PB_FILING_TYPE = {
    "P": "ptr",
    "A": "amendment",
    "O": "annual",
    "X": "annual",
    "C": "new_filer",
    "T": "termination",
}

SENATE_EFD_BASE_URL = "https://efdsearch.senate.gov"
SENATE_EFD_SEARCH_URL = f"{SENATE_EFD_BASE_URL}/search/"
SENATE_EFD_SEARCH_HOME_URL = f"{SENATE_EFD_BASE_URL}/search/home/"
SENATE_EFD_REPORT_DATA_URL = f"{SENATE_EFD_BASE_URL}/search/report/data/"

SENATE_REPORT_TYPE_PTR = "11"
SENATE_REPORT_TYPE_ANNUAL = "7"
SENATE_FILER_TYPE_SENATOR = "1"

CONGRESS_GOV_API_BASE = "https://api.congress.gov/v3"

REQUEST_TIMEOUT_SECONDS = 60
REQUEST_DELAY_SECONDS = 1.5

USER_AGENT = (
    "rollcalltrack.com research bot "
    "(financial-disclosure ingest; contact: hello@rollcalltrack.com)"
)


def require_config(dry_run: bool = False, source: str = "all"):
    missing = []
    if not dry_run:
        if not POCKETBASE_URL:
            missing.append("DATABASE_URL (PocketBase base URL, e.g. https://…/hcgi/platform)")
        if not POCKETBASE_ADMIN_EMAIL:
            missing.append("POCKETBASE_ADMIN_EMAIL")
        if not POCKETBASE_ADMIN_PASSWORD:
            missing.append("POCKETBASE_ADMIN_PASSWORD")
    if source in ("congress_enrich", "all") and not dry_run and not CONGRESS_GOV_API_KEY:
        missing.append("CONGRESS_GOV_API_KEY")
    if source == "congress_enrich" and dry_run and not CONGRESS_GOV_API_KEY:
        missing.append("CONGRESS_GOV_API_KEY")
    if missing:
        raise RuntimeError(
            f"Missing required environment variables: {', '.join(missing)}"
        )
