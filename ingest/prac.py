"""PRAC pandemic fraud enforcement ingest.

No official enforcement-action CSV/API (checked 2026-08). PRAC publishes
Investigative Results at pandemicoversight.gov as a Drupal listing that
links to DOJ press releases. /search/ is robots-disallowed; we only use
/oversight/reports?f[0]=report_type_taxonomy:85 (the Investigative Results
view) and do not fetch secondary news sites.

Dry-run is the default. Records whose source_url is not on the domain
allowlist, or whose program/status/defendant_type cannot be mapped
confidently, go to manual_review. Dollar amounts are taken only from
unhedged explicit figures.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import config
from .ingest_client import get_client

logger = logging.getLogger("rollcalltrack_ingest.prac")

BASE = "https://www.pandemicoversight.gov"
LISTING_PATH = "/oversight/reports"
LISTING_TYPE = "85"  # Investigative Results taxonomy
ALLOWLIST_PATH = Path(__file__).with_name("prac_source_allowlist.json")
PAGE_DELAY_SECONDS = 2
HORIZONS_BATCH_SIZE = 25

STATUS_ENUM = ("charged", "convicted", "settled", "ongoing")
DEFENDANT_ENUM = ("individual", "company", "multiple")

# Longer / more specific phrases first.
PROGRAM_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"paycheck protection program|\bppp\b", re.I), "Paycheck Protection Program (PPP)"),
    (re.compile(r"economic injury disaster loan|\beidl\b", re.I), "COVID-19 Economic Injury Disaster Loan (EIDL)"),
    (re.compile(r"pandemic unemployment assistance|\bpua\b", re.I), "Federal Pandemic Unemployment Assistance"),
    (re.compile(r"federal pandemic unemployment compensation|\bfpuc\b", re.I), "Federal Pandemic Unemployment Compensation"),
    (re.compile(r"unemployment insurance|\bunemployment benefits\b", re.I), "Unemployment Insurance"),
    (re.compile(r"employee retention credit|\berc\b", re.I), "Employee Retention Credit"),
    (re.compile(r"rental assistance|emergency rental", re.I), "COVID Rental Assistance"),
    (re.compile(r"shuttered venue", re.I), "Shuttered Venue Operators Grant"),
    (re.compile(r"restaurant revitalization", re.I), "Restaurant Revitalization Fund"),
    (re.compile(r"provider relief", re.I), "Provider Relief Fund"),
    (re.compile(r"economic impact payment|\bstimulus\b", re.I), "Economic Impact Payments"),
]

# Unhedged explicit amounts only. "over $40,000" / "more than $1 million" → None
# via HEDGE_RE (variable-width lookbehinds are not used).
EXACT_AMOUNT_RE = re.compile(
    r"\$\s*([\d,]+(?:\.\d+)?)\s*(million|billion)?",
    re.I,
)
HEDGE_RE = re.compile(
    r"\b(over|more than|up to|nearly|approximately|about|at least|almost|upwards of)\s+\$",
    re.I,
)

STATUS_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(sentenced|convicted|pleads guilty|pleaded guilty|pled guilty|guilty plea)\b", re.I), "convicted"),
    (re.compile(r"\b(settle|settles|settled|settlement|agreed to pay|agree to pay|resolve(?:s|d)? allegations)\b", re.I), "settled"),
    (re.compile(r"\b(indicted|charged|arrested|criminal complaint)\b", re.I), "charged"),
    (re.compile(r"\b(ongoing investigation|charges pending)\b", re.I), "ongoing"),
]


def load_allowlist(path: Path | None = None) -> list[str]:
    with (path or ALLOWLIST_PATH).open(encoding="utf-8") as f:
        data = json.load(f)
    return [d.lower().lstrip(".") for d in data.get("domains") or []]


def source_allowed(url: str, domains: list[str]) -> bool:
    host = (urlparse(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return False
    for d in domains:
        if host == d or host.endswith("." + d):
            return True
    return False


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


def iso_date(value: str | None) -> str | None:
    if not value:
        return None
    s = value.strip()
    if "T" in s:
        s = s.split("T", 1)[0]
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return s
    except ValueError:
        return None


def parse_exact_amount(text: str) -> float | None:
    """Return a single explicit unhedged dollar figure, or None."""
    if not text or HEDGE_RE.search(text):
        return None
    hits = []
    for m in EXACT_AMOUNT_RE.finditer(text):
        value = float(m.group(1).replace(",", ""))
        unit = (m.group(2) or "").lower()
        if unit == "billion":
            value *= 1_000_000_000
        elif unit == "million":
            value *= 1_000_000
        hits.append(value)
    unique = set(hits)
    if len(unique) == 1:
        return hits[0]
    return None


def map_program(text: str) -> str | None:
    for pat, name in PROGRAM_PATTERNS:
        if pat.search(text):
            return name
    return None


def map_status(text: str) -> str | None:
    for pat, status in STATUS_RULES:
        if pat.search(text):
            return status
    return None


def map_defendant_type(text: str) -> str | None:
    t = text.lower()
    company = bool(
        re.search(
            r"\b(llc|inc\.?|corp\.?|corporation|company|companies|subsidiary|"
            r"businesses|partnership)\b",
            t,
        )
    )
    multi_people = bool(
        re.search(r"\b(men|women|defendants|individuals|ring members?)\b", t)
    )
    individual = bool(
        re.search(
            r"\b(woman|man|employee|owner|resident|defendant)\b",
            t,
        )
    )
    if company and (individual or multi_people):
        return "multiple"
    if multi_people:
        return "multiple"
    if company:
        return "company"
    if individual:
        return "individual"
    return None


def parse_listing(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    rows = []
    for row in soup.select(".views-row"):
        title_el = row.select_one("h2 a[href], .display__condensed--title a[href]")
        if not title_el:
            continue
        href = (title_el.get("href") or "").strip()
        title = title_el.get_text(" ", strip=True)
        body_el = row.select_one(".field--name-body, .display__condensed--body")
        body = body_el.get_text(" ", strip=True) if body_el else ""
        time_el = row.select_one("time")
        issued = iso_date(time_el.get("datetime") if time_el else None)
        super_el = row.select_one(".display__condensed--superscript")
        publisher = super_el.get_text(" ", strip=True) if super_el else ""
        rows.append(
            {
                "case_title": title,
                "source_url": href,
                "body": body,
                "filing_date": issued,
                "publisher": publisher,
            }
        )
    return rows


def listing_has_next(html: str) -> bool:
    soup = BeautifulSoup(html, "lxml")
    for a in soup.select("a"):
        label = (a.get("aria-label") or a.get_text(" ", strip=True) or "").lower()
        if "next" in label:
            return True
    return False


def to_record(item: dict, allowlist: list[str]) -> dict:
    url = item.get("source_url") or ""
    title = item.get("case_title") or ""
    body = item.get("body") or ""
    blob = f"{title}. {body}"

    if not url:
        return {
            "_review": True,
            "reason": "missing_source_url",
            "notes": "listing row had no href",
            "case_title": title,
        }
    if not source_allowed(url, allowlist):
        host = urlparse(url).hostname or ""
        return {
            "_review": True,
            "reason": "unexpected_structure",
            "notes": f"source host {host!r} not on allowlist",
            "case_title": title,
            "source_url": url,
            "filing_date": item.get("filing_date"),
        }

    program = map_program(blob)
    status = map_status(blob)
    defendant = map_defendant_type(blob)
    missing = []
    if not program:
        missing.append("agency_program")
    if not status:
        missing.append("status")
    if not defendant:
        missing.append("defendant_type")
    if missing:
        return {
            "_review": True,
            "reason": "unexpected_structure",
            "notes": "could not confidently map: " + ", ".join(missing),
            "case_title": title,
            "source_url": url,
            "filing_date": item.get("filing_date"),
            "agency_program": program,
            "status": status,
            "defendant_type": defendant,
        }

    return {
        "case_title": title,
        "agency_program": program,
        "defendant_type": defendant,
        "amount_alleged": parse_exact_amount(blob),
        "status": status,
        "source_url": url,
        "filing_date": item.get("filing_date"),
    }


def fetch_listing(session: requests.Session, page: int) -> str:
    params = {
        "f[0]": f"report_type_taxonomy:{LISTING_TYPE}",
        "page": str(page),
    }
    url = f"{BASE}{LISTING_PATH}?{urlencode(params)}"
    logger.info("PRAC listing %s", url)
    resp = session.get(url, timeout=config.REQUEST_TIMEOUT_SECONDS)
    resp.raise_for_status()
    return resp.text


def run(
    days_back: int | None = None,
    max_records: int | None = None,
    dry_run: bool = True,
    client=None,
) -> dict:
    allowlist = load_allowlist()
    session = _session()
    window = days_back if days_back is not None else 14
    cutoff = (date.today() - timedelta(days=window)).isoformat()

    mapped: list[dict] = []
    review: list[dict] = []
    seen = 0
    page = 0
    stop = False
    while not stop:
        html = fetch_listing(session, page)
        rows = parse_listing(html)
        if not rows:
            break
        for item in rows:
            fd = item.get("filing_date")
            if fd and fd < cutoff:
                stop = True
                break
            rec = to_record(item, allowlist)
            seen += 1
            if rec.get("_review"):
                review.append(rec)
            else:
                mapped.append(rec)
            if max_records is not None and seen >= max_records:
                stop = True
                break
        if stop or not listing_has_next(html):
            break
        page += 1
        time.sleep(PAGE_DELAY_SECONDS)

    logger.info(
        "prac mapped=%s review=%s seen=%s days_back=%s dry_run=%s",
        len(mapped),
        len(review),
        seen,
        window,
        dry_run,
    )

    posted = 0
    if dry_run:
        for rec in mapped[:5]:
            logger.info("dry-run case %s", json.dumps(rec, default=str)[:500])
        for rec in review[:5]:
            logger.info("dry-run review %s", json.dumps(rec, default=str)[:500])
    else:
        hz = client or get_client()
        if not hz.configured:
            raise RuntimeError("HORIZONS_BASE_URL and INGEST_API_KEY required")
        for i in range(0, len(mapped), HORIZONS_BATCH_SIZE):
            hz.submit_fraud_cases(mapped[i : i + HORIZONS_BATCH_SIZE])
            posted += len(mapped[i : i + HORIZONS_BATCH_SIZE])
        for rec in review:
            hz.flag_manual_review(
                filing_id=rec.get("source_url") or rec.get("case_title") or "unknown",
                reason=rec.get("reason") or "unexpected_structure",
                notes=rec.get("notes") or "",
            )
        hz.submit_ingestion_log(
            source="prac",
            found=seen,
            parsed=len(mapped),
            failed=len(review),
            flagged=len(review),
            note=f"days_back={window} review_rate={len(review)}/{seen or 1}",
        )

    return {
        "found": seen,
        "mapped": len(mapped),
        "review": len(review),
        "posted": posted,
        "days_back": window,
        "dry_run": dry_run,
    }


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="PRAC pandemic fraud-case ingest")
    p.add_argument("--days-back", type=int, default=14)
    p.add_argument("--max-records", type=int, default=None)
    p.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=True,
        help="Parse only (default)",
    )
    p.add_argument(
        "--post",
        dest="dry_run",
        action="store_false",
        help="POST to Horizons (overrides default dry-run)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = build_arg_parser().parse_args(argv)
    try:
        config.require_config(dry_run=args.dry_run, source="prac")
    except RuntimeError as e:
        logger.error(str(e))
        return 1
    result = run(
        days_back=args.days_back,
        max_records=args.max_records,
        dry_run=args.dry_run,
    )
    logger.info("=== prac summary === %s", result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
