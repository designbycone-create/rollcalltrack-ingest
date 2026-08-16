"""DOJ / USAO fraud press ingest.

USAO district listing pages are bot-walled. This job only hits URLs that
return real HTML without a challenge: Criminal Division HCF case-summary
pages, national OPA press releases, and the public DOJ news RSS.

Records POST to the same /hcgi/api/ingest/spending/fraud-cases route as PRAC.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import config
from .ingest_client import get_client
from .prac import (
    load_allowlist,
    map_program,
    map_status,
    parse_exact_amount,
    source_allowed,
)

logger = logging.getLogger("rollcalltrack_ingest.doj_fraud")

SOURCES_PATH = Path(__file__).with_name("doj_fraud_sources.json")
RSS_URL = "https://www.justice.gov/news/rss?type=press_release&m=1"
HORIZONS_BATCH_SIZE = 25
TITLE_FILTER = re.compile(
    r"fraud|medicaid|feeding our future|child nutrition|"
    r"unemployment|paycheck protection|\bppp\b|\beidl\b",
    re.I,
)
SLUG_RE = re.compile(r"[^a-z0-9]+")


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": config.USER_AGENT})
    retry = Retry(
        total=4,
        connect=3,
        read=3,
        status=4,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


def slug_id(title: str) -> str:
    s = (title or "").casefold().strip()
    s = SLUG_RE.sub("-", s).strip("-")
    return s[:80] or "case"


def load_sources(path: Path | None = None) -> dict[str, Any]:
    return json.loads((path or SOURCES_PATH).read_text(encoding="utf-8"))


def map_defendant_type(text: str) -> str | None:
    t = (text or "").casefold()
    if re.search(r"\bet al\.?\b", t) or re.search(
        r"\b(and)\b.+\b(were charged|are charged)\b", t
    ):
        return "multiple"
    if re.search(r"\b(llc|inc\.?|corp\.?|company|companies)\b", t):
        return "company"
    if re.search(r"\b(men|women|defendants)\b", t):
        return "multiple"
    if re.search(r"\b(was charged|pleaded|pled|sentenced|woman|man)\b", t):
        return "individual"
    return None


def to_case(item: dict[str, Any], allowlist: list[str] | None = None) -> dict[str, Any]:
    allow = allowlist if allowlist is not None else load_allowlist()
    url = item.get("source_url") or ""
    title = item.get("case_title") or ""
    body = item.get("body") or ""
    blob = f"{title}. {body}"
    if not url or not source_allowed(url, allow):
        return {
            "_review": True,
            "reason": "unexpected_structure",
            "notes": "missing or disallowed source_url",
            "case_title": title,
            "source_url": url,
        }
    program = map_program(blob)
    status = map_status(blob) or "charged"
    defendant = map_defendant_type(blob)
    if not program or not defendant:
        missing = []
        if not program:
            missing.append("agency_program")
        if not defendant:
            missing.append("defendant_type")
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


def parse_hcf_summaries(
    html: str,
    *,
    page_url: str,
    announcement_date: str,
) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "lxml")
    rows: list[dict[str, Any]] = []
    for h in soup.find_all(["h2", "h3", "h4"]):
        title = h.get_text(" ", strip=True)
        if not title.startswith("U.S. v."):
            continue
        bits = []
        for sib in h.next_siblings:
            if getattr(sib, "name", None) in ("h2", "h3", "h4"):
                break
            if getattr(sib, "get_text", None):
                bits.append(sib.get_text(" ", strip=True))
        body = " ".join(b for b in bits if b)
        rows.append(
            {
                "case_title": title,
                "body": body,
                "source_url": f"{page_url.split('#')[0]}#{slug_id(title)}",
                "filing_date": announcement_date,
            }
        )
    return rows


def parse_rss_items(xml_text: str, days_back: int) -> list[dict[str, Any]]:
    root = ET.fromstring(xml_text)
    cutoff = date.today() - timedelta(days=days_back)
    out: list[dict[str, Any]] = []
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        raw_date = item.findtext("pubDate") or ""
        if not title or not link or not TITLE_FILTER.search(title):
            continue
        if "memorandum" in title.casefold() or "priorities" in title.casefold():
            continue
        filing = None
        try:
            filing = parsedate_to_datetime(raw_date).date().isoformat()
        except (TypeError, ValueError):
            filing = None
        if filing and filing < cutoff.isoformat():
            continue
        out.append(
            {
                "case_title": title,
                "body": title,
                "source_url": link,
                "filing_date": filing,
            }
        )
    return out


def run(
    *,
    days_back: int = 400,
    max_records: int | None = None,
    dry_run: bool = True,
    client=None,
) -> dict[str, Any]:
    allow = load_allowlist()
    session = _session()
    sources = load_sources()
    raw: list[dict[str, Any]] = []

    for listing in sources.get("listings") or []:
        url = listing.get("url")
        kind = listing.get("kind")
        announced = listing.get("announcement_date") or date.today().isoformat()
        if not url:
            continue
        resp = session.get(url, timeout=config.REQUEST_TIMEOUT_SECONDS)
        if resp.status_code != 200 or "bm-verify" in resp.text:
            logger.warning("skip listing HTTP %s %s", resp.status_code, url)
            continue
        if kind == "hcf_summaries":
            raw.extend(
                parse_hcf_summaries(
                    resp.text, page_url=url, announcement_date=announced
                )
            )
        elif kind == "opa_pr":
            soup = BeautifulSoup(resp.text, "lxml")
            title = soup.find("h1")
            body = soup.get_text(" ", strip=True)[:4000]
            raw.append(
                {
                    "case_title": (title.get_text(" ", strip=True) if title else url),
                    "body": body,
                    "source_url": url,
                    "filing_date": announced,
                }
            )

    try:
        rss = session.get(RSS_URL, timeout=config.REQUEST_TIMEOUT_SECONDS)
        if rss.status_code == 200:
            raw.extend(parse_rss_items(rss.text, days_back))
    except requests.RequestException as exc:
        logger.warning("rss failed: %s", exc)

    mapped: list[dict] = []
    review: list[dict] = []
    seen_urls: set[str] = set()
    for item in raw:
        url = item.get("source_url") or ""
        if url in seen_urls:
            continue
        seen_urls.add(url)
        rec = to_case(item, allow)
        if rec.get("_review"):
            review.append(rec)
        else:
            mapped.append(rec)
        if max_records is not None and (len(mapped) + len(review)) >= max_records:
            break

    posted = 0
    if dry_run:
        for rec in mapped[:5]:
            logger.info("dry-run case %s", json.dumps(rec)[:400])
        for rec in review[:5]:
            logger.info("dry-run review %s", json.dumps(rec)[:400])
    elif mapped:
        hz = client or get_client()
        for i in range(0, len(mapped), HORIZONS_BATCH_SIZE):
            hz.submit_fraud_cases(mapped[i : i + HORIZONS_BATCH_SIZE])
            posted += len(mapped[i : i + HORIZONS_BATCH_SIZE])

    result = {
        "found": len(raw),
        "mapped": len(mapped),
        "review": len(review),
        "posted": posted,
        "days_back": days_back,
        "dry_run": dry_run,
    }
    logger.info("=== doj-fraud summary === %s", result)
    return result


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    p = argparse.ArgumentParser(description="DOJ fraud press ingest")
    p.add_argument("--days-back", type=int, default=400)
    p.add_argument("--max-records", type=int, default=None)
    p.add_argument("--dry-run", dest="dry_run", action="store_true", default=True)
    p.add_argument("--post", dest="dry_run", action="store_false")
    args = p.parse_args(argv)
    try:
        config.require_config(dry_run=args.dry_run, source="prac")
    except RuntimeError as e:
        logger.error(str(e))
        return 1
    run(
        days_back=args.days_back,
        max_records=args.max_records,
        dry_run=args.dry_run,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
