"""FOIA.gov agency-component ingest → public_records_offices.

Source: GET https://api.foia.gov/api/agency_components?include=agency
Auth: FOIA_GOV_API_KEY (api.data.gov) as header X-API-Key.

Each active component becomes a jurisdiction=federal office. Submit
stays on FOIA.gov; we do not file requests.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from typing import Any

import requests

from . import config
from .ingest_client import get_client

logger = logging.getLogger("rollcalltrack_ingest.foia_federal")

FOIA_COMPONENTS_URL = "https://api.foia.gov/api/agency_components"
FOIA_REQUEST_URL = "https://www.foia.gov/request"
PAGE_LIMIT = 50
HORIZONS_BATCH_SIZE = 25


def api_key() -> str:
    return (
        os.environ.get("FOIA_GOV_API_KEY")
        or getattr(config, "FOIA_GOV_API_KEY", "")
        or ""
    )


_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def unique_slug(abbr: str, foia_id: str, used: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", (abbr or "").strip().lower())
    base = re.sub(r"-{2,}", "-", base).strip("-")
    if not _SLUG_RE.fullmatch(base or "") or len(base) < 2:
        hexid = re.sub(r"[^a-z0-9]", "", foia_id.lower())[:12]
        base = f"office-{hexid}" if hexid else "office"
    if base not in used:
        used.add(base)
        return base
    suffix = re.sub(r"[^a-z0-9]", "", foia_id.lower())[:6] or "x"
    slug = f"{base}-{suffix}"
    n = 2
    while slug in used or not _SLUG_RE.fullmatch(slug):
        slug = f"{base}-{suffix}{n}"
        n += 1
    used.add(slug)
    return slug


def _uri(obj: Any) -> str:
    if isinstance(obj, dict):
        return (obj.get("uri") or "").strip()
    if isinstance(obj, str):
        return obj.strip()
    return ""


def _first_email(raw: Any) -> str:
    if isinstance(raw, list) and raw:
        return str(raw[0]).strip()
    if isinstance(raw, str):
        return raw.strip()
    return ""


def _format_address(addr: Any) -> str:
    if not isinstance(addr, dict):
        return ""
    parts = [
        addr.get("address_line1"),
        addr.get("address_line2"),
        addr.get("locality"),
        addr.get("administrative_area"),
        addr.get("postal_code"),
    ]
    return ", ".join(str(p).strip() for p in parts if p)


def map_component(item: dict, agencies: dict[str, dict]) -> dict | None:
    attrs = item.get("attributes") or {}
    if attrs.get("status") is False:
        return None
    foia_id = item.get("id") or ""
    if not foia_id:
        return None
    rel = ((item.get("relationships") or {}).get("agency") or {}).get("data") or {}
    agency = agencies.get(rel.get("id") or "") or {}
    parent = (agency.get("attributes") or {}).get("name") or ""
    website = _uri(attrs.get("website"))
    submission_web = _uri(attrs.get("submission_web"))
    rooms = attrs.get("reading_rooms") or []
    reading = _uri(rooms[0]) if rooms else ""
    title = (attrs.get("title") or "").strip()
    if not title:
        return None
    return {
        "slug": "",  # filled in run() after uniqueness
        "jurisdiction": "federal",
        "state_code": None,
        "parent_agency": parent or title,
        "office_name": title,
        "abbreviation": (attrs.get("abbreviation") or "").strip(),
        "foia_email": _first_email(attrs.get("email")),
        "request_url": submission_web or FOIA_REQUEST_URL,
        "website": website,
        "reading_room_url": reading,
        "submission_methods": (attrs.get("portal_submission_format") or "").strip(),
        "mail_address": _format_address(attrs.get("submission_address")),
        "official_form_url": submission_web or None,
        "source_url": f"https://api.foia.gov/api/agency_components/{foia_id}",
        "foia_gov_id": foia_id,
        "_abbr": (attrs.get("abbreviation") or "").strip(),
    }


def fetch_all(session: requests.Session, key: str) -> tuple[list[dict], dict[str, dict]]:
    headers = {
        "X-API-Key": key,
        "Accept": "application/vnd.api+json",
    }
    url = FOIA_COMPONENTS_URL
    params: dict[str, Any] = {"include": "agency", "page[limit]": PAGE_LIMIT}
    components: list[dict] = []
    agencies: dict[str, dict] = {}
    while url:
        resp = session.get(url, headers=headers, params=params, timeout=config.REQUEST_TIMEOUT_SECONDS)
        params = None
        resp.raise_for_status()
        payload = resp.json()
        for row in payload.get("data") or []:
            components.append(row)
        for row in payload.get("included") or []:
            rid = row.get("id")
            if rid:
                agencies[rid] = row
        nxt = ((payload.get("links") or {}).get("next") or {}).get("href")
        url = nxt or ""
    return components, agencies


def run(*, dry_run: bool = False, max_records: int | None = None, client=None) -> dict:
    key = api_key()
    if not key:
        raise RuntimeError("FOIA_GOV_API_KEY is required")
    session = requests.Session()
    session.headers.update({"User-Agent": config.USER_AGENT})
    components, agencies = fetch_all(session, key)
    used: set[str] = set()
    offices: list[dict] = []
    skipped = 0
    for item in components:
        rec = map_component(item, agencies)
        if rec is None:
            skipped += 1
            continue
        rec["slug"] = unique_slug(rec.pop("_abbr"), rec["foia_gov_id"], used)
        offices.append(rec)
        if max_records is not None and len(offices) >= max_records:
            break
    posted = 0
    if dry_run:
        for rec in offices[:5]:
            logger.info("dry-run office %s", json.dumps(rec, default=str)[:400])
    else:
        hz = client or get_client()
        if not hz.configured:
            raise RuntimeError("HORIZONS_BASE_URL and INGEST_API_KEY required")
        for i in range(0, len(offices), HORIZONS_BATCH_SIZE):
            batch = offices[i : i + HORIZONS_BATCH_SIZE]
            hz.submit_public_records_offices(batch)
            posted += len(batch)
    summary = {
        "found": len(components),
        "mapped": len(offices),
        "skipped": skipped,
        "posted": posted if not dry_run else 0,
        "dry_run": dry_run,
    }
    logger.info("=== foia federal summary === %s", summary)
    return summary


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    p = argparse.ArgumentParser(description="Ingest FOIA.gov agency components")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--max-records", type=int, default=None)
    args = p.parse_args()
    run(dry_run=args.dry_run, max_records=args.max_records)
    return 0


if __name__ == "__main__":
    sys.exit(main())
