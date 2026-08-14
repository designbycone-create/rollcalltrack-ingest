"""California Open FI$Cal vendor-expenditure ingest.

Official portal: https://open.fiscal.ca.gov/  (CA GOV §11862)

These are modified-accrual AP / GL expenditure lines with vendor names,
NOT contracts, grants, loans, or federal-style awards. This job does not
set award_type or amount_type. See README.
"""
from __future__ import annotations

import argparse
import csv
import io
import logging
import sys
import time
from calendar import monthrange
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import config
from .ingest_client import get_client

logger = logging.getLogger("rollcalltrack_ingest.ca_spending")

PORTAL = "https://open.fiscal.ca.gov"
DOWNLOAD_PAGE = f"{PORTAL}/monthly_vendor_transaction.html"
POINTER_URL = (
    "https://adwoutputfilesadlsstore.blob.core.windows.net/transparency/"
    "MonthlyVendorTransactionPointer/MonthlyVendorTransactionPointer.csv"
)

HORIZONS_BATCH_SIZE = 25
REVIEW_FLAG_CAP = 25
FILE_DELAY_SECONDS = 1.5

# Open FI$Cal has no per-voucher permalink. This locator is the closest
# public handle: portal download page + the line's natural keys.
SOURCE_URL_PATH = f"{DOWNLOAD_PAGE}"


def california_fiscal_year(d: date | None = None) -> int:
    """CA FY N runs 1 July N through 30 June N+1. FY25 = 2025-26."""
    d = d or date.today()
    return d.year if d.month >= 7 else d.year - 1


def fiscal_year_bounds(fy: int) -> tuple[date, date]:
    return date(fy, 7, 1), date(fy + 1, 6, 30)


def period_bounds(fy: int, period: int) -> tuple[date, date]:
    """P01 = July of fy … P12 = June of fy+1."""
    if period < 1 or period > 12:
        raise ValueError(f"accounting period must be 1-12, got {period}")
    month = ((period - 1 + 6) % 12) + 1  # 1→7, 6→12, 7→1, 12→6
    year = fy if period <= 6 else fy + 1
    last = monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last)


def parse_vendor_filename(name: str) -> tuple[int, int] | None:
    """Vendor_FY25P11.csv → (2025, 11). Spending_* files are ignored."""
    s = (name or "").strip()
    if not s.upper().startswith("VENDOR_FY") or not s.lower().endswith(".csv"):
        return None
    stem = s[len("Vendor_FY") :]
    if stem.lower().endswith(".csv"):
        stem = stem[:-4]
    # FY25P11
    if "P" not in stem.upper():
        return None
    up = stem.upper()
    fy_part, per_part = up.split("P", 1)
    if not fy_part.isdigit() or not per_part.isdigit():
        return None
    fy = int(fy_part)
    if fy < 100:
        fy += 2000
    return fy, int(per_part)


def iso_date(value: Any) -> str | None:
    if value is None or value == "":
        return None
    s = str(value).strip()
    if not s:
        return None
    if "T" in s:
        s = s.split("T", 1)[0]
    if " " in s:
        s = s.split(" ", 1)[0]
    try:
        datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        return None
    return s


def _cell(row: dict, *names: str) -> str:
    lower = {str(k).strip().lower(): v for k, v in row.items()}
    for name in names:
        v = lower.get(name.lower())
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""


def parse_amount(raw: Any) -> float | None:
    if raw is None or raw == "":
        return None
    s = str(raw).strip().replace(",", "").replace("$", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def source_url_for(
    document_id: str,
    account: str,
    accounting_date: str,
    program_code: str,
) -> str:
    qs = urlencode(
        {
            "document_id": document_id,
            "account": account,
            "accounting_date": accounting_date,
            "program_code": program_code,
        }
    )
    return f"{SOURCE_URL_PATH}?{qs}"


def files_for_window(
    pointer_rows: list[dict],
    fiscal_year: int,
    since: date | None = None,
    until: date | None = None,
) -> list[dict]:
    """Vendor files for a CA FY whose period overlaps [since, until]."""
    until = until or date.today()
    out: list[dict] = []
    for row in pointer_rows:
        name = _cell(row, "FileName", "filename")
        parsed = parse_vendor_filename(name)
        if not parsed:
            continue
        fy, period = parsed
        if fy != fiscal_year:
            continue
        p_start, p_end = period_bounds(fy, period)
        if since and p_end < since:
            continue
        if p_start > until:
            continue
        url = _cell(row, "Download", "url")
        if not url:
            continue
        out.append(
            {
                "filename": name,
                "url": url,
                "fiscal_year": fy,
                "period": period,
                "period_start": p_start,
                "period_end": p_end,
                "upload_date": _cell(row, "UploadDate", "upload_date"),
            }
        )
    out.sort(key=lambda f: (f["fiscal_year"], f["period"]), reverse=True)
    return out


def map_row(row: dict, file_url: str = "") -> dict:
    """Map one vendor CSV line. Review records have ``_review`` set.

    Does not set award_type or amount_type — Open FI$Cal lines are posted
    expenditures, not federal awards.
    """
    document_id = _cell(row, "document_id")
    vendor = _cell(row, "VENDOR_NAME", "vendor_name")
    account = _cell(row, "account")
    program_code = _cell(row, "program_code")
    program = _cell(row, "program_description")
    acct_desc = _cell(row, "account_description")
    agency = _cell(row, "department_name") or _cell(row, "agency_name")
    acct_date = iso_date(_cell(row, "accounting_date"))
    amount = parse_amount(_cell(row, "monetary_amount"))
    fy_raw = _cell(row, "fiscal_year_begin")

    locator = source_url_for(
        document_id=document_id or "missing",
        account=account,
        accounting_date=acct_date or "",
        program_code=program_code,
    )

    if not document_id:
        return {
            "_review": True,
            "reason": "unexpected_structure",
            "notes": "missing document_id",
            "source_url": locator,
        }

    award_id = f"CA-{document_id}-{account}-{program_code}-{acct_date or 'nodate'}"

    if vendor.upper() == "CONFIDENTIAL":
        return {
            "_review": True,
            "reason": "unexpected_structure",
            "notes": (
                "Vendor name is Confidential (Open FI$Cal masks employees "
                "and legally protected payees). Not posted as a recipient."
            ),
            "award_id": award_id,
            "source_url": locator,
            "awarding_agency": agency,
        }

    if amount is None:
        return {
            "_review": True,
            "reason": "unexpected_structure",
            "notes": f"unparseable monetary_amount {row.get('monetary_amount')!r}",
            "award_id": award_id,
            "source_url": locator,
        }

    if amount <= 0:
        return {
            "_review": True,
            "reason": "unexpected_structure",
            "notes": (
                f"monetary_amount={amount} is a reversal/adjustment, "
                "not an obligated award amount. Not posted."
            ),
            "award_id": award_id,
            "source_url": locator,
        }

    if not acct_date:
        return {
            "_review": True,
            "reason": "unexpected_structure",
            "notes": f"unparseable accounting_date {row.get('accounting_date')!r}",
            "award_id": award_id,
            "source_url": locator,
        }

    if not vendor:
        return {
            "_review": True,
            "reason": "unexpected_structure",
            "notes": "missing vendor name",
            "award_id": award_id,
            "source_url": locator,
        }

    try:
        fy = int(str(fy_raw).strip())
    except ValueError:
        fy = None
    if fy is None:
        return {
            "_review": True,
            "reason": "unexpected_structure",
            "notes": f"unparseable fiscal_year_begin {fy_raw!r}",
            "award_id": award_id,
            "source_url": locator,
        }

    desc_parts = [p for p in (program, acct_desc) if p]
    rec = {
        "award_id": award_id,
        "recipient_name": vendor,
        "recipient_state": None,
        "awarding_agency": agency,
        "amount": amount,
        "period_of_performance_start": acct_date,
        "period_of_performance_end": None,
        "description": " — ".join(desc_parts),
        "source_url": locator,
        "fiscal_year": fy,
        "jurisdiction": "state",
        "state_code": "CA",
        "program": program,
    }
    if file_url:
        rec["ca_source_file"] = file_url
    return rec


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": config.USER_AGENT})
    retry = Retry(
        total=5,
        connect=3,
        read=3,
        status=5,
        backoff_factor=2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def fetch_pointer(session: requests.Session) -> list[dict]:
    logger.info("Fetching Open FI$Cal vendor pointer")
    resp = session.get(POINTER_URL, timeout=config.REQUEST_TIMEOUT_SECONDS)
    if resp.status_code != 200:
        logger.error("Pointer HTTP %s: %s", resp.status_code, resp.text[:300])
        resp.raise_for_status()
    text = resp.content.decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


def iter_vendor_rows(session: requests.Session, url: str):
    logger.info("Streaming vendor file %s", url)
    resp = session.get(
        url, stream=True, timeout=max(config.REQUEST_TIMEOUT_SECONDS, 180)
    )
    if resp.status_code != 200:
        logger.error("Vendor file HTTP %s %s", resp.status_code, url)
        resp.raise_for_status()
    resp.raw.decode_content = True
    wrapper = io.TextIOWrapper(resp.raw, encoding="utf-8-sig", newline="")
    try:
        yield from csv.DictReader(wrapper)
    finally:
        wrapper.detach()
        resp.close()


def run(
    fiscal_year: int,
    days_back: int | None = None,
    max_records: int | None = None,
    dry_run: bool = False,
    client=None,
) -> dict:
    session = _session()
    pointer = fetch_pointer(session)
    until = date.today()
    since = (until - timedelta(days=days_back)) if days_back is not None else None
    files = files_for_window(
        pointer, fiscal_year=fiscal_year, since=since, until=until
    )
    logger.info(
        "ca-spending fy=%s files=%s since=%s",
        fiscal_year,
        [f["filename"] for f in files],
        since,
    )

    mapped: list[dict] = []
    review: list[dict] = []
    found = 0
    remaining = max_records

    for i, meta in enumerate(files):
        if remaining is not None and remaining <= 0:
            break
        if i:
            time.sleep(FILE_DELAY_SECONDS)
        for row in iter_vendor_rows(session, meta["url"]):
            if remaining is not None and remaining <= 0:
                break
            acct_date = iso_date(_cell(row, "accounting_date"))
            if since and acct_date:
                try:
                    if datetime.strptime(acct_date, "%Y-%m-%d").date() < since:
                        continue
                except ValueError:
                    pass
            found += 1
            if remaining is not None:
                remaining -= 1
            rec = map_row(row, file_url=meta["url"])
            if rec.get("_review"):
                review.append(rec)
            else:
                mapped.append(rec)

    logger.info(
        "ca-spending mapped=%s review=%s found=%s fy=%s",
        len(mapped),
        len(review),
        found,
        fiscal_year,
    )

    posted = 0
    flagged = 0
    if dry_run:
        for rec in mapped[:3]:
            logger.info("dry-run award %s", rec)
        for rec in review[:3]:
            logger.info("dry-run review %s", rec)
    else:
        hz = client or get_client()
        if not hz.configured:
            raise RuntimeError("HORIZONS_BASE_URL and INGEST_API_KEY required")
        for i in range(0, len(mapped), HORIZONS_BATCH_SIZE):
            batch = mapped[i : i + HORIZONS_BATCH_SIZE]
            hz.submit_spending_awards(batch)
            posted += len(batch)
        for rec in review[:REVIEW_FLAG_CAP]:
            hz.flag_manual_review(
                filing_id=rec.get("award_id") or "unknown",
                reason=rec.get("reason") or "unexpected_structure",
                notes=rec.get("notes") or "",
            )
            flagged += 1
        extra = max(0, len(review) - REVIEW_FLAG_CAP)
        hz.submit_ingestion_log(
            source="ca_spending",
            found=found,
            parsed=len(mapped),
            failed=0,
            flagged=len(review),
            note=(
                f"fy={fiscal_year} files={len(files)} "
                f"review_uncapped={len(review)} flagged={flagged} "
                f"review_omitted={extra}"
            ),
        )

    return {
        "found": found,
        "mapped": len(mapped),
        "review": len(review),
        "posted": posted,
        "flagged": flagged,
        "fiscal_year": fiscal_year,
        "files": [f["filename"] for f in files],
        "dry_run": dry_run,
    }


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="California Open FI$Cal spending ingest")
    p.add_argument(
        "--fiscal-year",
        type=int,
        required=True,
        help="California FY begin year (2025 = FY25 = 2025-07-01 … 2026-06-30)",
    )
    p.add_argument("--days-back", type=int, default=None)
    p.add_argument("--max-records", type=int, default=None)
    p.add_argument("--dry-run", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = build_arg_parser().parse_args(argv)
    try:
        config.require_config(dry_run=args.dry_run, source="ca_spending")
    except RuntimeError as e:
        logger.error(str(e))
        return 1
    if args.dry_run:
        logger.info("Dry run — no Horizons writes")
    result = run(
        fiscal_year=args.fiscal_year,
        days_back=args.days_back,
        max_records=args.max_records,
        dry_run=args.dry_run,
    )
    logger.info("=== ca-spending summary === %s", result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
