"""Environment configuration. Loads from .env locally, from real env vars
(GitHub Actions secrets) in CI.

Primary write path: Horizons HTTP API
  HORIZONS_BASE_URL  e.g. https://<app-id>.app-preview.com
  INGEST_API_KEY     sent as X-Ingest-Key

Legacy/optional: PocketBase superuser (direct collection writes) still
supported if INGEST_API_KEY is not set.
"""
import os
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()

# Horizons site origin (no path). Prefer explicit HORIZONS_BASE_URL.
_raw_db = os.environ.get("DATABASE_URL") or os.environ.get("POCKETBASE_URL") or ""
_raw_horizons = os.environ.get("HORIZONS_BASE_URL") or os.environ.get("HORIZONS_BASE") or ""


def _origin_from_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if not url:
        return ""
    # If path includes /hcgi/platform, strip to origin
    if "://" in url:
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}"
    return url


if _raw_horizons:
    HORIZONS_BASE_URL = _origin_from_url(_raw_horizons)
elif _raw_db.startswith("http"):
    HORIZONS_BASE_URL = _origin_from_url(_raw_db)
else:
    HORIZONS_BASE_URL = ""

INGEST_API_KEY = (
    os.environ.get("INGEST_API_KEY")
    or os.environ.get("INGEST_KEY")
    or os.environ.get("X_INGEST_KEY")
    or os.environ.get("X-Ingest-Key")
    or ""
)

# Legacy PocketBase direct access (optional)
DATABASE_URL = _raw_db
POCKETBASE_URL = _raw_db
POCKETBASE_ADMIN_EMAIL = os.environ.get("POCKETBASE_ADMIN_EMAIL")
POCKETBASE_ADMIN_PASSWORD = os.environ.get("POCKETBASE_ADMIN_PASSWORD")

CONGRESS_GOV_API_KEY = os.environ.get("CONGRESS_GOV_API_KEY")

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


def use_horizons_api() -> bool:
    return bool(HORIZONS_BASE_URL and INGEST_API_KEY)


def require_config(dry_run: bool = False, source: str = "all"):
    missing = []
    if not dry_run:
        if use_horizons_api():
            pass  # HORIZONS_BASE_URL + INGEST_API_KEY present
        else:
            # Fall back to PocketBase superuser path
            if not POCKETBASE_URL:
                missing.append(
                    "INGEST_API_KEY (+ HORIZONS_BASE_URL or DATABASE_URL), "
                    "or DATABASE_URL for legacy PocketBase"
                )
            if not POCKETBASE_ADMIN_EMAIL:
                missing.append("POCKETBASE_ADMIN_EMAIL (or set INGEST_API_KEY)")
            if not POCKETBASE_ADMIN_PASSWORD:
                missing.append("POCKETBASE_ADMIN_PASSWORD (or set INGEST_API_KEY)")
    # congress_enrich is skipped under Horizons API mode (roster owned by Horizons).
    needs_congress = not CONGRESS_GOV_API_KEY and (
        source == "congress_enrich"
        or (source == "all" and not dry_run and not use_horizons_api())
    )
    if needs_congress:
        missing.append("CONGRESS_GOV_API_KEY")
    if missing:
        raise RuntimeError(
            f"Missing required environment variables: {', '.join(missing)}"
        )
