"""GAO / PaymentAccuracy improper-payment ingest.

GAO does not publish a machine-readable program-level export. Their annual
Payment Integrity report (latest: GAO-26-108694) has incomplete PDF tables.
GAO's own analysis is of PaymentAccuracy.gov — HTML program pages that
publish official dollar amounts by cause. This job reads those published
figures and attributes them to the matching GAO report in gao_reports.json.

We never compute amount = outlays * rate.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import config
from .ingest_client import get_client

logger = logging.getLogger("rollcalltrack_ingest.gao")

PAYMENTACCURACY_BASE = "https://paymentaccuracy.gov"
AGENCIES_INDEX = f"{PAYMENTACCURACY_BASE}/agencies-and-programs"
REPORTS_PATH = Path(__file__).with_name("gao_reports.json")
PAGE_DELAY_SECONDS = 1.5
HORIZONS_BATCH_SIZE = 25
CAUSE_ENUM = (
    "overpayment",
    "underpayment",
    "fraud",
    "documentation_error",
    "other",
)

# Label fragments on PaymentAccuracy program pages → cause_category.
# Order matters: first match wins. Fraud is only used if a dollar line
# is explicitly labeled fraud — never inferred from overpayment.
CAUSE_LABELS: list[tuple[str, str]] = [
    ("amount of overpayments within the agency", "overpayment"),
    ("amount of overpayments outside the agency", "overpayment"),
    ("amount of underpayments", "underpayment"),
    ("technically improper", "documentation_error"),
    ("insufficient or lack of documentation", "other"),
    ("unknown payment amount", "other"),
]

MONEY_RE = re.compile(
    r"\$\s*([\d,]+(?:\.\d+)?)\s*(billion|million|b|m)?",
    re.I,
)


def load_report_catalog(path: Path | None = None) -> dict:
    with (path or REPORTS_PATH).open(encoding="utf-8") as f:
        return json.load(f)


def require_report(catalog: dict, fiscal_year: int) -> dict:
    rec = catalog.get(str(fiscal_year))
    if not rec:
        known = ", ".join(sorted(catalog)) or "(none)"
        raise ValueError(
            f"No GAO Payment Integrity report mapped for FY {fiscal_year}. "
            f"Add it to ingest/gao_reports.json. Known years: {known}"
        )
    for key in ("report_id", "title", "url", "report_date"):
        if not rec.get(key):
            raise ValueError(f"gao_reports.json FY {fiscal_year} missing {key}")
    return rec


def parse_money_to_dollars(text: str) -> float | None:
    """Parse a published '$35,029.54 M' style figure. No invented units."""
    if not text:
        return None
    m = MONEY_RE.search(text.replace("\xa0", " "))
    if not m:
        return None
    value = float(m.group(1).replace(",", ""))
    unit = (m.group(2) or "").lower()
    if unit in ("billion", "b"):
        return value * 1_000_000_000
    if unit in ("million", "m"):
        return value * 1_000_000
    return value


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip().lower()


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


def discover_program_urls(html: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    seen: list[str] = []
    found: set[str] = set()
    for a in soup.select('a[href*="/program/"]'):
        href = (a.get("href") or "").strip()
        if not href:
            continue
        url = urljoin(PAYMENTACCURACY_BASE + "/", href)
        if url not in found:
            found.add(url)
            seen.append(url)
    return seen


def parse_program_page(html: str, fiscal_year: int, page_url: str) -> dict:
    """Extract agency, program name, and published cause amounts for one FY."""
    soup = BeautifulSoup(html, "lxml")
    program_name = ""
    crumb = soup.select_one(".breadcrumbs, nav.usa-breadcrumb, p.breadcrumbs")
    if crumb:
        bits = [
            t.strip(" >\t")
            for t in crumb.stripped_strings
            if t.strip(" >\t") and t.strip() not in (">", "Home", "Agencies & Programs")
        ]
        if bits:
            program_name = bits[-1].lstrip("> ").strip()
    if not program_name or program_name.lower().startswith("program level"):
        for h1 in soup.find_all("h1"):
            name = " ".join(h1.stripped_strings).strip()
            if name and not name.lower().startswith("program level"):
                program_name = name.lstrip("> ").strip()
                break

    agency = ""
    for h3 in soup.find_all("h3"):
        text = " ".join(h3.stripped_strings)
        m = re.match(r"Sponsoring agency:\s*(.+)", text, re.I)
        if m:
            agency = m.group(1).strip()
            break

    year_block = None
    for div in soup.select(f'[data-year="{fiscal_year}"]'):
        blob = _norm(div.get_text(" ", strip=True))
        if "amount of overpayments" in blob or "improper payment estimates" in blob:
            year_block = div
            break
    if year_block is None:
        return {
            "agency": agency,
            "program_name": program_name,
            "page_url": page_url,
            "causes": {},
            "error": f"no FY {fiscal_year} estimates block",
        }

    causes: dict[str, float] = {}
    # Walk labeled amounts inside the FY block.
    text_nodes = year_block.find_all(string=True)
    labels_and_values: list[tuple[str, str]] = []
    pending_label = ""
    for node in text_nodes:
        t = " ".join(str(node).split())
        if not t:
            continue
        if MONEY_RE.search(t) and pending_label:
            labels_and_values.append((pending_label, t))
            pending_label = ""
            continue
        if any(key in _norm(t) for key, _ in CAUSE_LABELS):
            pending_label = t

    for label, raw in labels_and_values:
        nlabel = _norm(label)
        amount = parse_money_to_dollars(raw)
        if amount is None:
            continue
        for key, cause in CAUSE_LABELS:
            if key in nlabel:
                causes[cause] = causes.get(cause, 0.0) + amount
                break

    return {
        "agency": agency,
        "program_name": program_name,
        "page_url": page_url,
        "causes": causes,
        "error": None,
    }


def records_from_parse(parsed: dict, fiscal_year: int, report: dict) -> list[dict]:
    out: list[dict] = []
    if parsed.get("error"):
        return [
            {
                "_review": True,
                "reason": "unexpected_structure",
                "notes": parsed["error"],
                "program_name": parsed.get("program_name") or "",
                "page_url": parsed.get("page_url") or "",
            }
        ]
    if not parsed.get("agency") or not parsed.get("program_name"):
        return [
            {
                "_review": True,
                "reason": "unexpected_structure",
                "notes": "missing agency or program_name",
                "page_url": parsed.get("page_url") or "",
            }
        ]
    for cause, amount in parsed["causes"].items():
        if cause not in CAUSE_ENUM:
            continue
        if amount == 0:
            continue
        out.append(
            {
                "agency": parsed["agency"],
                "program_name": parsed["program_name"],
                "fiscal_year": fiscal_year,
                "estimated_amount_low": amount,
                "estimated_amount_high": amount,
                "cause_category": cause,
                "source_report_title": report["title"],
                "source_url": report["url"],
                "report_date": report["report_date"],
            }
        )
    if not out:
        return [
            {
                "_review": True,
                "reason": "unexpected_structure",
                "notes": (
                    f"no non-zero published cause amounts for FY {fiscal_year}"
                ),
                "agency": parsed.get("agency") or "",
                "program_name": parsed.get("program_name") or "",
                "page_url": parsed.get("page_url") or "",
            }
        ]
    return out


def run(
    fiscal_year: int,
    dry_run: bool = False,
    max_records: int | None = None,
    client=None,
) -> dict:
    catalog = load_report_catalog()
    report = require_report(catalog, fiscal_year)
    session = _session()
    logger.info("Fetching program index %s", AGENCIES_INDEX)
    idx = session.get(AGENCIES_INDEX, timeout=config.REQUEST_TIMEOUT_SECONDS)
    idx.raise_for_status()
    urls = discover_program_urls(idx.text)
    logger.info("Discovered %d program pages", len(urls))
    if max_records is not None:
        urls = urls[:max_records]

    mapped: list[dict] = []
    review: list[dict] = []
    for url in urls:
        logger.info("Fetching %s", url)
        resp = session.get(url, timeout=config.REQUEST_TIMEOUT_SECONDS)
        if resp.status_code != 200:
            review.append(
                {
                    "_review": True,
                    "reason": "unexpected_structure",
                    "notes": f"HTTP {resp.status_code}",
                    "page_url": url,
                }
            )
            time.sleep(PAGE_DELAY_SECONDS)
            continue
        parsed = parse_program_page(resp.text, fiscal_year, url)
        recs = records_from_parse(parsed, fiscal_year, report)
        for rec in recs:
            if rec.get("_review"):
                review.append(rec)
            else:
                mapped.append(rec)
        time.sleep(PAGE_DELAY_SECONDS)

    logger.info(
        "gao mapped=%s review=%s programs=%s fy=%s report=%s",
        len(mapped),
        len(review),
        len(urls),
        fiscal_year,
        report["report_id"],
    )

    posted = 0
    if dry_run:
        for rec in mapped[:5]:
            logger.info("dry-run record %s", json.dumps(rec, default=str)[:500])
        for rec in review[:5]:
            logger.info("dry-run review %s", json.dumps(rec, default=str)[:500])
    else:
        hz = client or get_client()
        if not hz.configured:
            raise RuntimeError("HORIZONS_BASE_URL and INGEST_API_KEY required")
        for i in range(0, len(mapped), HORIZONS_BATCH_SIZE):
            hz.submit_improper_payments(mapped[i : i + HORIZONS_BATCH_SIZE])
            posted += len(mapped[i : i + HORIZONS_BATCH_SIZE])
        for rec in review:
            hz.flag_manual_review(
                filing_id=rec.get("program_name") or rec.get("page_url") or "unknown",
                reason=rec.get("reason") or "unexpected_structure",
                notes=rec.get("notes") or "",
            )
        hz.submit_ingestion_log(
            source="gao",
            found=len(urls),
            parsed=len(mapped),
            failed=len(review),
            flagged=len(review),
            note=f"fy={fiscal_year} report={report['report_id']}",
        )

    return {
        "found": len(urls),
        "mapped": len(mapped),
        "review": len(review),
        "posted": posted,
        "fiscal_year": fiscal_year,
        "report_id": report["report_id"],
        "dry_run": dry_run,
    }


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="GAO improper-payment ingest")
    p.add_argument("--fiscal-year", type=int, required=True)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--max-records",
        type=int,
        default=None,
        help="Cap program pages (testing only)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = build_arg_parser().parse_args(argv)
    try:
        config.require_config(dry_run=args.dry_run, source="gao")
    except RuntimeError as e:
        logger.error(str(e))
        return 1
    try:
        result = run(
            fiscal_year=args.fiscal_year,
            dry_run=args.dry_run,
            max_records=args.max_records,
        )
    except ValueError as e:
        logger.error(str(e))
        return 1
    logger.info("=== gao summary === %s", result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
