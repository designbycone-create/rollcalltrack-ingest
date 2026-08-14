"""Oversight.gov Inspector General report ingest.

No public reports API (checked 2026-08): /jsonapi, /api/reports, and
_format=json all 404/406. Site RSS is homepage news, not reports.

Public HTML listing at /reports/federal is allowed by robots.txt.
Disallowed: /search/, /admin/, /user/*. Crawl-delay: 5 seconds.

Listing + report pages expose Drupal fields: agency, type, date,
recommendation count, questioned costs, funds for better use.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import urlencode, urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import config
from .ingest_client import get_client

logger = logging.getLogger("rollcalltrack_ingest.oversight")

BASE = "https://www.oversight.gov"
LISTING_PATH = "/reports/federal"
CRAWL_DELAY_SECONDS = 5
PAGE_SIZE = 100
HORIZONS_BATCH_SIZE = 25

REPORT_TYPE_ENUM = ("audit", "investigation", "evaluation", "semiannual_report")

# Exact listing/detail labels → enum. Anything else goes to review.
TYPE_MAP = {
    "audit": "audit",
    "investigation": "investigation",
    "inspection / evaluation": "evaluation",
    "inspection/evaluation": "evaluation",
    "evaluation": "evaluation",
    "semiannual report": "semiannual_report",
    "semiannual reports": "semiannual_report",
}

MONEY_RE = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)")
SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'])")


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
    s.mount("https://", adapter := HTTPAdapter(max_retries=retry))
    s.mount("http://", adapter)
    return s


def map_report_type(raw: str | None) -> str | None:
    if not raw:
        return None
    key = re.sub(r"\s+", " ", raw).strip().lower()
    return TYPE_MAP.get(key)


def parse_money(text: str | None) -> float | None:
    """Parse an explicit '$1,234' figure. Does not sum or infer."""
    if not text:
        return None
    m = MONEY_RE.search(text.replace("\xa0", " "))
    if not m:
        return None
    return float(m.group(1).replace(",", ""))


def parse_int(text: str | None) -> int | None:
    if not text:
        return None
    m = re.search(r"(\d+)", text.replace(",", ""))
    return int(m.group(1)) if m else None


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
        pass
    for fmt in ("%m/%d/%Y", "%B %d, %Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def financial_impact_from_fields(questioned: float | None, better_use: float | None) -> float | None:
    """Use an explicit figure only when it is unambiguous.

    Questioned costs and funds-for-better-use are different concepts.
    If both are present and differ, we do not add them.
    """
    present = [v for v in (questioned, better_use) if v is not None]
    if not present:
        return None
    if len(set(present)) == 1:
        return present[0]
    return None


def summarize(text: str) -> str:
    """First 2–3 sentences. No added adjectives."""
    text = re.sub(r"\s+", " ", (text or "")).strip()
    if not text:
        return ""
    parts = SENTENCE_RE.split(text)
    parts = [p.strip() for p in parts if p.strip()]
    return " ".join(parts[:3])


def resolve_agency_option(html: str, agency_flag: str) -> tuple[str, str]:
    """Return (option_value, official_label) for --agency."""
    soup = BeautifulSoup(html, "lxml")
    sel = soup.find("select", attrs={"name": "field_report_agency_reviewed[]"})
    if not sel:
        raise ValueError("Agency filter not found on Oversight listing")
    needle = agency_flag.strip().lower()
    hits: list[tuple[str, str]] = []
    for opt in sel.find_all("option"):
        value = (opt.get("value") or "").strip()
        label = opt.get_text(" ", strip=True)
        if not value or not label:
            continue
        low = label.lower()
        if needle == low or needle == value.lower() or needle in low:
            hits.append((value, label))
    exact = [h for h in hits if h[1].lower() == needle]
    if len(exact) == 1:
        return exact[0]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise ValueError(f"No Oversight agency matches {agency_flag!r}")
    labels = ", ".join(h[1] for h in hits[:8])
    raise ValueError(f"Ambiguous --agency {agency_flag!r}: {labels}")


def parse_listing(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    rows = []
    table = soup.select_one("table.listing-table")
    if not table:
        return rows
    for tr in table.select("tbody tr"):
        if "listing_table__row--accent" in (tr.get("class") or []):
            continue
        cells = {
            (td.get("data-label") or "").strip(): td
            for td in tr.find_all("td")
        }
        if "Report Title" not in cells:
            continue
        time_el = cells.get("Report Date").find("time") if cells.get("Report Date") else None
        issued = None
        if time_el and time_el.get("datetime"):
            issued = iso_date(time_el["datetime"])
        if not issued and cells.get("Report Date"):
            issued = iso_date(cells["Report Date"].get_text(" ", strip=True))
        link = tr.find("a", href=True)
        href = link["href"] if link else ""
        if href and not href.startswith("http"):
            href = urljoin(BASE, href)
        rows.append(
            {
                "agency": (cells.get("Agency Reviewed / Investigated").get_text(" ", strip=True)
                           if cells.get("Agency Reviewed / Investigated") else ""),
                "report_title": cells["Report Title"].get_text(" ", strip=True),
                "report_type_raw": (
                    cells.get("Type").get_text(" ", strip=True) if cells.get("Type") else ""
                ),
                "publish_date": issued,
                "source_url": href,
            }
        )
    return rows


def listing_has_next(html: str) -> bool:
    soup = BeautifulSoup(html, "lxml")
    for a in soup.select("a.usa-pagination__button, li.usa-pagination__item a"):
        if (a.get_text(" ", strip=True) or "").lower() in ("next", "last") or "next" in (
            a.get("aria-label") or ""
        ).lower():
            return True
        rel = a.get("rel") or []
        if "next" in rel:
            return True
    # Drupal uses ?page=N; current page is usa-current
    current = soup.select_one("a.usa-current")
    if current:
        siblings = current.find_parent("ul")
        if siblings:
            links = siblings.select("a.usa-pagination__button")
            if links and current != links[-1]:
                return True
    return False


def parse_detail(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")

    def field(name: str) -> str:
        el = soup.select_one(f".field--name-{name}")
        return el.get_text(" ", strip=True) if el else ""

    recs = parse_int(field("field-report-number-of-recs"))
    qc = parse_money(field("field-net-questioned-costs"))
    fbu = parse_money(field("field-net-funds-for-better-use"))
    # Only treat as present if the field actually existed.
    if not field("field-net-questioned-costs"):
        qc = None
    if not field("field-net-funds-for-better-use"):
        fbu = None

    body = soup.select_one(".field--name-body")
    rec_ps = []
    if not body:
        for p in soup.select("main p"):
            t = p.get_text(" ", strip=True)
            if t.lower().startswith("we recommend") or len(t) > 80:
                rec_ps.append(t)
    text = body.get_text(" ", strip=True) if body else " ".join(rec_ps)

    agency = field("field-report-agency-reviewed")
    agency = re.sub(r"^Agencies Reviewed/Investigated\s*", "", agency, flags=re.I).strip()
    rtype = field("field-report-type")
    rtype = re.sub(r"^Report Type\s*", "", rtype, flags=re.I).strip()

    issued_raw = field("field-report-date-issued")
    issued = None
    time_el = soup.select_one(".field--name-field-report-date-issued time")
    if time_el and time_el.get("datetime"):
        issued = iso_date(time_el["datetime"])
    if not issued:
        issued = iso_date(re.sub(r"^Date Issued\s*", "", issued_raw, flags=re.I))

    title = ""
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(" ", strip=True)

    return {
        "agency": agency,
        "report_title": title,
        "report_type_raw": rtype,
        "publish_date": issued,
        "recommendations_count": recs,
        "financial_impact": financial_impact_from_fields(qc, fbu),
        "summary": summarize(text),
        "questioned_costs": qc,
        "funds_for_better_use": fbu,
    }


def to_record(listing: dict, detail: dict | None) -> dict:
    agency = (detail or {}).get("agency") or listing.get("agency") or ""
    title = (detail or {}).get("report_title") or listing.get("report_title") or ""
    raw_type = (detail or {}).get("report_type_raw") or listing.get("report_type_raw") or ""
    rtype = map_report_type(raw_type)
    pub = (detail or {}).get("publish_date") or listing.get("publish_date")
    url = listing.get("source_url") or ""

    if not url:
        return {
            "_review": True,
            "reason": "missing_source_url",
            "notes": "listing row had no report URL",
            "report_title": title,
        }
    if not agency or rtype is None:
        return {
            "_review": True,
            "reason": "unexpected_structure",
            "notes": f"unmapped agency={agency!r} type={raw_type!r}",
            "agency": agency,
            "report_title": title,
            "source_url": url,
            "publish_date": pub,
        }

    rec: dict[str, Any] = {
        "agency": agency,
        "report_title": title,
        "report_type": rtype,
        "financial_impact": (detail or {}).get("financial_impact"),
        "summary": (detail or {}).get("summary") or "",
        "recommendations_count": (detail or {}).get("recommendations_count"),
        "source_url": url,
        "publish_date": pub,
    }
    return rec


def fetch_listing_page(
    session: requests.Session,
    page: int,
    date_min: str,
    date_max: str,
    agency_id: str | None,
) -> str:
    params: list[tuple[str, str]] = [
        ("items_per_page", str(PAGE_SIZE)),
        ("field_report_date_issued[min]", date_min),
        ("field_report_date_issued[max]", date_max),
        ("page", str(page)),
    ]
    if agency_id:
        params.append(("field_report_agency_reviewed[]", agency_id))
    url = f"{BASE}{LISTING_PATH}?{urlencode(params)}"
    logger.info("Oversight listing %s", url)
    resp = session.get(url, timeout=config.REQUEST_TIMEOUT_SECONDS)
    resp.raise_for_status()
    return resp.text


def run(
    days_back: int | None = None,
    agency: str | None = None,
    max_records: int | None = None,
    dry_run: bool = False,
    client=None,
) -> dict:
    session = _session()
    today = date.today()
    window = days_back if days_back is not None else 14
    date_min = (today - timedelta(days=window)).isoformat()
    date_max = today.isoformat()

    html = fetch_listing_page(session, 0, date_min, date_max, None)
    agency_id = None
    if agency:
        agency_id, official = resolve_agency_option(html, agency)
        logger.info("Agency filter %r → id=%s (%s)", agency, agency_id, official)
        time.sleep(CRAWL_DELAY_SECONDS)
        html = fetch_listing_page(session, 0, date_min, date_max, agency_id)

    listings: list[dict] = []
    page = 0
    while True:
        listings.extend(parse_listing(html))
        if max_records is not None and len(listings) >= max_records:
            listings = listings[:max_records]
            break
        if not listing_has_next(html):
            break
        page += 1
        time.sleep(CRAWL_DELAY_SECONDS)
        html = fetch_listing_page(session, page, date_min, date_max, agency_id)

    mapped: list[dict] = []
    review: list[dict] = []
    for item in listings:
        time.sleep(CRAWL_DELAY_SECONDS)
        detail = None
        if item.get("source_url"):
            try:
                logger.info("Oversight detail %s", item["source_url"])
                resp = session.get(item["source_url"], timeout=config.REQUEST_TIMEOUT_SECONDS)
                if resp.status_code == 200:
                    detail = parse_detail(resp.text)
                else:
                    logger.warning("detail HTTP %s %s", resp.status_code, item["source_url"])
            except requests.RequestException as e:
                logger.warning("detail failed %s: %s", item.get("source_url"), e)
        rec = to_record(item, detail)
        if rec.get("_review"):
            review.append(rec)
        else:
            mapped.append(rec)

    logger.info(
        "oversight mapped=%s review=%s listed=%s days_back=%s",
        len(mapped),
        len(review),
        len(listings),
        window,
    )

    posted = 0
    if dry_run:
        for rec in mapped[:5]:
            logger.info("dry-run report %s", json.dumps(rec, default=str)[:500])
        for rec in review[:5]:
            logger.info("dry-run review %s", json.dumps(rec, default=str)[:500])
    else:
        hz = client or get_client()
        if not hz.configured:
            raise RuntimeError("HORIZONS_BASE_URL and INGEST_API_KEY required")
        for i in range(0, len(mapped), HORIZONS_BATCH_SIZE):
            hz.submit_ig_reports(mapped[i : i + HORIZONS_BATCH_SIZE])
            posted += len(mapped[i : i + HORIZONS_BATCH_SIZE])
        for rec in review:
            hz.flag_manual_review(
                filing_id=rec.get("source_url") or rec.get("report_title") or "unknown",
                reason=rec.get("reason") or "unexpected_structure",
                notes=rec.get("notes") or "",
            )
        hz.submit_ingestion_log(
            source="oversight",
            found=len(listings),
            parsed=len(mapped),
            failed=len(review),
            flagged=len(review),
            note=f"days_back={window} agency={agency or 'all'}",
        )

    return {
        "found": len(listings),
        "mapped": len(mapped),
        "review": len(review),
        "posted": posted,
        "days_back": window,
        "dry_run": dry_run,
    }


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Oversight.gov IG report ingest")
    p.add_argument("--days-back", type=int, default=14)
    p.add_argument("--agency", default=None, help="Agency name (substring or exact)")
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
        config.require_config(dry_run=args.dry_run, source="oversight")
    except RuntimeError as e:
        logger.error(str(e))
        return 1
    try:
        result = run(
            days_back=args.days_back,
            agency=args.agency,
            max_records=args.max_records,
            dry_run=args.dry_run,
        )
    except ValueError as e:
        logger.error(str(e))
        return 1
    logger.info("=== oversight summary === %s", result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
