"""50-state spending orchestrator (state-spending-sync).

Each state has a row in ``state_portals.json``. Only adapters with a
confirmed bulk CSV/API run. HTML checkbooks are not scraped.

Posted state expenditures are not federal awards. This job never sets
``award_type`` or ``amount_type``.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import ca_spending, config
from .ingest_client import get_client
from .legiscan import STATE_CODES, resolve_states

logger = logging.getLogger("rollcalltrack_ingest.state_spending")

CATALOG_PATH = Path(__file__).with_name("state_portals.json")
HORIZONS_BATCH_SIZE = 25
REVIEW_FLAG_CAP = 25
PAGE_SIZE = 1000
SODA_DELAY_SECONDS = 1.0


def load_catalog(path: Path | None = None) -> dict:
    p = path or CATALOG_PATH
    with p.open(encoding="utf-8") as f:
        return json.load(f)


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


def _cell(row: dict, key: str) -> str:
    if not key:
        return ""
    if key in row and row[key] is not None and str(row[key]).strip():
        return str(row[key]).strip()
    lower = {str(k).strip().lower(): v for k, v in row.items()}
    v = lower.get(key.lower())
    if v is not None and str(v).strip():
        return str(v).strip()
    return ""


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
    if s.isdigit() and len(s) == 4:
        return f"{s}-07-01"
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


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


def map_generic_row(
    row: dict,
    spec: dict,
    state_code: str,
    fiscal_year: int,
) -> dict:
    """Map a vendor-payment line. Never sets award_type / amount_type."""
    fields = spec.get("map") or {}
    vendor = _cell(row, fields.get("vendor") or "")
    amount = parse_amount(_cell(row, fields.get("amount") or ""))
    agency = _cell(row, fields.get("agency") or "")
    program = _cell(row, fields.get("program") or "")
    acct_date = iso_date(_cell(row, fields.get("date") or ""))
    if not acct_date:
        acct_date = iso_date(_cell(row, spec.get("fiscal_year_field") or ""))
    native_id = _cell(row, fields.get("id") or "") or acct_date or vendor
    portal = (spec.get("portal_url") or "").rstrip("/")
    qs = urlencode(
        {
            "document_id": native_id,
            "accounting_date": acct_date or "",
            "state": state_code,
        }
    )
    source_url = f"{portal}?{qs}" if portal else ""

    award_id = f"{state_code}-{native_id}-{acct_date or 'nodate'}"

    if not native_id:
        return {
            "_review": True,
            "reason": "unexpected_structure",
            "notes": "missing native id",
            "source_url": source_url,
        }
    if vendor.upper() in ("CONFIDENTIAL", "UNDISCLOSED", "NOT PROVIDED", "REDACTED"):
        return {
            "_review": True,
            "reason": "unexpected_structure",
            "notes": "Vendor name is Confidential. Not posted as a recipient.",
            "award_id": award_id,
            "source_url": source_url,
        }
    if amount is None:
        return {
            "_review": True,
            "reason": "unexpected_structure",
            "notes": f"unparseable amount {row.get(fields.get('amount'))!r}",
            "award_id": award_id,
            "source_url": source_url,
        }
    if amount <= 0:
        return {
            "_review": True,
            "reason": "unexpected_structure",
            "notes": f"amount={amount} is not a positive posted expenditure.",
            "award_id": award_id,
            "source_url": source_url,
        }
    if not acct_date:
        return {
            "_review": True,
            "reason": "unexpected_structure",
            "notes": "unparseable date",
            "award_id": award_id,
            "source_url": source_url,
        }
    if not vendor:
        return {
            "_review": True,
            "reason": "unexpected_structure",
            "notes": "missing vendor",
            "award_id": award_id,
            "source_url": source_url,
        }
    return {
        "award_id": award_id,
        "recipient_name": vendor,
        "recipient_state": None,
        "awarding_agency": agency,
        "amount": amount,
        "period_of_performance_start": acct_date,
        "period_of_performance_end": None,
        "description": program,
        "source_url": source_url,
        "fiscal_year": fiscal_year,
        "jurisdiction": "state",
        "state_code": state_code,
        "program": program,
    }


def _filter_date(row: dict, spec: dict, since: date | None) -> bool:
    if since is None:
        return True
    fields = spec.get("map") or {}
    d = iso_date(_cell(row, fields.get("date") or ""))
    if not d:
        return True
    return datetime.strptime(d, "%Y-%m-%d").date() >= since


def fetch_socrata(
    session: requests.Session,
    spec: dict,
    since: date | None,
    remaining: int | None,
    fiscal_year: int | None = None,
) -> list[dict]:
    domain = spec["soda_domain"]
    dataset = spec["soda_dataset"]
    date_field = (spec.get("map") or {}).get("date")
    fy_field = spec.get("fiscal_year_field")
    out: list[dict] = []
    offset = 0
    while True:
        if remaining is not None and remaining <= 0:
            break
        limit = PAGE_SIZE
        if remaining is not None:
            limit = min(PAGE_SIZE, remaining)
        order = f"{date_field} DESC" if date_field else ":id"
        params = {"$limit": limit, "$offset": offset, "$order": order}
        wheres = []
        if since and date_field and date_field != fy_field:
            wheres.append(f"{date_field} >= '{since.isoformat()}'")
        if date_field and date_field != fy_field:
            wheres.append(f"{date_field} IS NOT NULL")
        if fiscal_year and fy_field:
            wheres.append(f"{fy_field} = '{fiscal_year}'")
        if wheres:
            params["$where"] = " AND ".join(wheres)
        url = f"https://{domain}/resource/{dataset}.json"
        resp = session.get(
            url, params=params, timeout=config.REQUEST_TIMEOUT_SECONDS
        )
        if resp.status_code != 200:
            logger.error("SODA HTTP %s %s: %s", resp.status_code, url, resp.text[:300])
            resp.raise_for_status()
        batch = resp.json()
        if not isinstance(batch, list) or not batch:
            break
        out.extend(batch)
        if remaining is not None:
            remaining -= len(batch)
        if len(batch) < limit:
            break
        offset += len(batch)
        time.sleep(SODA_DELAY_SECONDS)
    return out


def fetch_ckan(
    session: requests.Session,
    spec: dict,
    since: date | None,
    remaining: int | None,
) -> list[dict]:
    """Resolve the newest matching resource on a CKAN package, then read CSV."""
    show = spec["ckan_package_show"]
    needle = (spec.get("ckan_resource_contains") or "").lower()
    resp = session.get(show, timeout=config.REQUEST_TIMEOUT_SECONDS)
    resp.raise_for_status()
    resources = ((resp.json().get("result") or {}).get("resources")) or []
    hits = []
    for res in resources:
        name = (res.get("name") or res.get("url") or "").lower()
        if needle and needle not in name:
            continue
        if not (res.get("url") or "").endswith(".csv"):
            continue
        hits.append(res)
    if not hits:
        raise RuntimeError(f"No CKAN CSV resource matching {needle!r} at {show}")
    hits.sort(key=lambda r: r.get("last_modified") or r.get("created") or "", reverse=True)
    url = hits[0]["url"]
    logger.info("CKAN latest resource %s", url)
    return fetch_csv(session, {**spec, "csv_url": url}, since, remaining)


def fetch_csv(
    session: requests.Session,
    spec: dict,
    since: date | None,
    remaining: int | None,
) -> list[dict]:
    url = spec["csv_url"]
    resp = session.get(url, timeout=max(config.REQUEST_TIMEOUT_SECONDS, 180))
    if resp.status_code != 200:
        logger.error("CSV HTTP %s %s", resp.status_code, url)
        resp.raise_for_status()
    text = None
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = resp.content.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise UnicodeDecodeError("utf-8", resp.content, 0, 1, "no working codec")
    rows = []
    for row in csv.DictReader(io.StringIO(text)):
        if not _filter_date(row, spec, since):
            continue
        rows.append(row)
        if remaining is not None and len(rows) >= remaining:
            break
    return rows


def run_state(
    code: str,
    spec: dict,
    fiscal_year: int,
    days_back: int | None,
    max_records: int | None,
    dry_run: bool,
    client,
) -> dict:
    adapter = spec.get("adapter") or "unsupported"
    if adapter == "unsupported":
        logger.info("%s skipped — no confirmed bulk source (%s)", code, spec.get("notes", "")[:160])
        return {
            "status": "unsupported",
            "found": 0,
            "mapped": 0,
            "review": 0,
            "posted": 0,
        }
    if adapter == "open_fiscal":
        inner = ca_spending.run(
            fiscal_year=fiscal_year,
            days_back=days_back,
            max_records=max_records,
            dry_run=dry_run,
            client=client,
        )
        inner["status"] = "ok"
        return inner

    session = _session()
    until = date.today()
    since = (until - timedelta(days=days_back)) if days_back is not None else None
    if adapter == "socrata":
        rows = fetch_socrata(
            session, spec, since, max_records, fiscal_year=fiscal_year
        )
    elif adapter == "csv":
        rows = fetch_csv(session, spec, since, max_records)
    elif adapter == "ckan":
        rows = fetch_ckan(session, spec, since, max_records)
    else:
        raise ValueError(f"{code}: unknown adapter {adapter}")

    mapped: list[dict] = []
    review: list[dict] = []
    for row in rows:
        rec = map_generic_row(row, spec, code, fiscal_year)
        if rec.get("_review"):
            review.append(rec)
        else:
            mapped.append(rec)

    posted = 0
    flagged = 0
    if dry_run:
        for rec in mapped[:2]:
            logger.info("dry-run %s award %s", code, rec)
        for rec in review[:2]:
            logger.info("dry-run %s review %s", code, rec)
    else:
        hz = client or get_client()
        if not hz.configured:
            raise RuntimeError("HORIZONS_BASE_URL and INGEST_API_KEY required")
        for i in range(0, len(mapped), HORIZONS_BATCH_SIZE):
            hz.submit_spending_awards(mapped[i : i + HORIZONS_BATCH_SIZE])
            posted += min(HORIZONS_BATCH_SIZE, len(mapped) - i)
        for rec in review[:REVIEW_FLAG_CAP]:
            hz.flag_manual_review(
                filing_id=rec.get("award_id") or "unknown",
                reason=rec.get("reason") or "unexpected_structure",
                notes=rec.get("notes") or "",
            )
            flagged += 1
        hz.submit_ingestion_log(
            source="ca_spending",
            found=len(rows),
            parsed=len(mapped),
            failed=0,
            flagged=len(review),
            note=f"state={code} adapter={adapter} fy={fiscal_year}",
        )

    return {
        "status": "ok",
        "found": len(rows),
        "mapped": len(mapped),
        "review": len(review),
        "posted": posted,
        "flagged": flagged,
    }


def run(
    states: list[str],
    fiscal_year: int,
    days_back: int | None = None,
    max_records: int | None = None,
    dry_run: bool = False,
    client=None,
) -> dict:
    catalog = load_catalog()
    per: dict[str, dict] = {}
    for raw in states:
        code = raw.upper()
        if code not in STATE_CODES:
            raise ValueError(f"Not a 50-state USPS code: {raw}")
        spec = catalog["states"][code]
        logger.info("state-spending %s adapter=%s fy=%s", code, spec.get("adapter"), fiscal_year)
        per[code] = run_state(
            code, spec, fiscal_year, days_back, max_records, dry_run, client
        )

    return {
        "found": sum(v.get("found", 0) for v in per.values()),
        "mapped": sum(v.get("mapped", 0) for v in per.values()),
        "review": sum(v.get("review", 0) for v in per.values()),
        "posted": sum(v.get("posted", 0) for v in per.values()),
        "fiscal_year": fiscal_year,
        "dry_run": dry_run,
        "states": per,
    }


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="50-state spending transparency ingest")
    p.add_argument("--state", default=None, help="Single 2-letter USPS code")
    p.add_argument("--all-states", action="store_true")
    p.add_argument(
        "--fiscal-year",
        type=int,
        required=True,
        help="State FY begin year where the portal uses one (CA: 2025 = FY25)",
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
        config.require_config(dry_run=args.dry_run, source="state_spending")
        states = resolve_states(args.state, args.all_states)
    except (RuntimeError, ValueError) as e:
        logger.error(str(e))
        return 1
    result = run(
        states=states,
        fiscal_year=args.fiscal_year,
        days_back=args.days_back,
        max_records=args.max_records,
        dry_run=args.dry_run,
    )
    logger.info("=== state-spending summary === %s", result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
