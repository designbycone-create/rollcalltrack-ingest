"""USAspending.gov award ingest — contracts, grants, loans, direct payments.

Live-verified (2026-08) against api.usaspending.gov:
  POST /api/v2/search/spending_by_award/
  GET  /api/v2/references/award_types/
  GET  /api/v2/references/toptier_agencies/

Award Amount on contracts/grants/direct payments/insurance is the official
obligated figure. Loans do not expose Award Amount — they expose Loan Value
(face) and Subsidy Cost. Those are not posted as obligated ``amount``.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import config
from .ingest_client import get_client

logger = logging.getLogger("rollcalltrack_ingest.usaspending")

USASPENDING_SEARCH_URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
USASPENDING_AWARD_PAGE = "https://www.usaspending.gov/award/{award_id}"
AGENCIES_PATH = Path(__file__).with_name("usaspending_agencies.json")

PAGE_SIZE = 100
HORIZONS_BATCH_SIZE = 25
SEARCH_DELAY_SECONDS = 1.5

AWARD_TYPE_ENUM = ("contract", "grant", "loan", "direct_payment", "insurance")

# Search groups must stay separate: field sets differ by award family.
# Codes from GET /api/v2/references/award_types/ (2026-08).
AWARD_GROUPS: dict[str, dict[str, Any]] = {
    "contract": {
        "codes": ["A", "B", "C", "D"],
        "amount_field": "Award Amount",
        "start_field": "Start Date",
        "end_field": "End Date",
        "fields": [
            "Award ID",
            "Recipient Name",
            "Recipient Location",
            "Start Date",
            "End Date",
            "Award Amount",
            "Awarding Agency",
            "Contract Award Type",
            "Description",
            "generated_internal_id",
            "Last Modified Date",
        ],
    },
    "grant": {
        "codes": ["02", "03", "04", "05"],
        "amount_field": "Award Amount",
        "start_field": "Start Date",
        "end_field": "End Date",
        "fields": [
            "Award ID",
            "Recipient Name",
            "Recipient Location",
            "Start Date",
            "End Date",
            "Award Amount",
            "Awarding Agency",
            "Award Type",
            "Description",
            "generated_internal_id",
            "Last Modified Date",
        ],
    },
    "direct_payment": {
        "codes": ["06", "10"],
        "amount_field": "Award Amount",
        "start_field": "Start Date",
        "end_field": "End Date",
        "fields": [
            "Award ID",
            "Recipient Name",
            "Recipient Location",
            "Start Date",
            "End Date",
            "Award Amount",
            "Awarding Agency",
            "Award Type",
            "Description",
            "generated_internal_id",
            "Last Modified Date",
        ],
    },
    "insurance": {
        "codes": ["09"],
        "amount_field": "Award Amount",
        "start_field": "Start Date",
        "end_field": "End Date",
        "fields": [
            "Award ID",
            "Recipient Name",
            "Recipient Location",
            "Start Date",
            "End Date",
            "Award Amount",
            "Awarding Agency",
            "Award Type",
            "Description",
            "generated_internal_id",
            "Last Modified Date",
        ],
    },
    "loan": {
        "codes": ["07", "08"],
        "amount_field": None,
        "start_field": "Issued Date",
        "end_field": None,
        "fields": [
            "Award ID",
            "Recipient Name",
            "Recipient Location",
            "Issued Date",
            "Loan Value",
            "Subsidy Cost",
            "Awarding Agency",
            "Description",
            "generated_internal_id",
            "Last Modified Date",
        ],
    },
}


def load_agency_catalog(path: Path | None = None) -> dict:
    p = path or AGENCIES_PATH
    with p.open(encoding="utf-8") as f:
        return json.load(f)


def resolve_agencies(
    catalog: dict,
    agency_flag: str | None = None,
    use_default_allowlist: bool = True,
) -> list[dict]:
    """Resolve --agency (abbrev or full name) or the default allowlist."""
    by_abbr = {k.upper(): v for k, v in (catalog.get("agencies") or {}).items()}
    by_name = {v["name"].lower(): v for v in by_abbr.values()}

    if agency_flag:
        key = agency_flag.strip()
        hit = by_abbr.get(key.upper()) or by_name.get(key.lower())
        if not hit:
            known = ", ".join(sorted(by_abbr))
            raise ValueError(
                f"Unknown agency {agency_flag!r}. "
                f"Add it to ingest/usaspending_agencies.json or use one of: {known}"
            )
        return [hit]

    if not use_default_allowlist:
        return list(by_abbr.values())

    out = []
    for abbr in catalog.get("default_allowlist") or []:
        if abbr.upper() not in by_abbr:
            raise ValueError(f"Allowlist agency {abbr!r} missing from agencies map")
        out.append(by_abbr[abbr.upper()])
    if not out:
        raise ValueError("default_allowlist is empty")
    return out


def federal_fiscal_year(d: date | None = None) -> int:
    d = d or date.today()
    return d.year + 1 if d.month >= 10 else d.year


def fiscal_year_bounds(fy: int) -> tuple[date, date]:
    """FY N runs Oct 1 of (N-1) through Sep 30 of N."""
    return date(fy - 1, 10, 1), date(fy, 9, 30)


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


def recipient_state(row: dict) -> str | None:
    loc = row.get("Recipient Location") or {}
    if isinstance(loc, str):
        return loc.strip().upper()[:2] or None
    code = (loc.get("state_code") or "").strip().upper()
    if len(code) == 2 and code.isalpha():
        return code
    return None


def source_url_for(generated_internal_id: str) -> str:
    return USASPENDING_AWARD_PAGE.format(
        award_id=quote(generated_internal_id, safe="-._~")
    )


def map_award(row: dict, award_type: str, fiscal_year: int) -> dict:
    """Map a spending_by_award row to the Horizons payload, or a review record.

    Review records have ``_review`` set and must not be POSTed as awards.
    """
    group = AWARD_GROUPS[award_type]
    gid = (row.get("generated_internal_id") or "").strip()
    if not gid:
        return {
            "_review": True,
            "reason": "unexpected_structure",
            "notes": "missing generated_internal_id",
            "raw_award_id": row.get("Award ID"),
        }

    if award_type not in AWARD_TYPE_ENUM:
        return {
            "_review": True,
            "reason": "unexpected_structure",
            "notes": f"award_type {award_type!r} not in enum",
            "award_id": gid,
            "source_url": source_url_for(gid),
        }

    amount_field = group["amount_field"]
    if amount_field is None:
        # Loans: do not relabel Loan Value / Subsidy Cost as obligated.
        return {
            "_review": True,
            "reason": "unexpected_structure",
            "notes": (
                "Loan has no obligated Award Amount. "
                f"loan_value={row.get('Loan Value')!r} "
                f"subsidy_cost={row.get('Subsidy Cost')!r}. "
                "Not posted as amount/obligated."
            ),
            "award_id": gid,
            "source_url": source_url_for(gid),
            "recipient_name": row.get("Recipient Name") or "",
            "fiscal_year": fiscal_year,
        }

    if amount_field not in row or row[amount_field] is None:
        return {
            "_review": True,
            "reason": "unexpected_structure",
            "notes": f"missing {amount_field} on {award_type}",
            "award_id": gid,
            "source_url": source_url_for(gid),
        }

    start = iso_date(row.get(group["start_field"]))
    end = iso_date(row.get(group["end_field"])) if group["end_field"] else None
    if not start:
        return {
            "_review": True,
            "reason": "unexpected_structure",
            "notes": f"unparseable start date {row.get(group['start_field'])!r}",
            "award_id": gid,
            "source_url": source_url_for(gid),
        }

    return {
        "award_id": gid,
        "recipient_name": row.get("Recipient Name") or "",
        "recipient_state": recipient_state(row),
        "awarding_agency": row.get("Awarding Agency") or "",
        "award_type": award_type,
        "amount": row[amount_field],
        "amount_type": "obligated",
        "period_of_performance_start": start,
        "period_of_performance_end": end,
        "description": row.get("Description") or "",
        "source_url": source_url_for(gid),
        "fiscal_year": fiscal_year,
    }


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": config.USER_AGENT,
            "Content-Type": "application/json",
        }
    )
    retry = Retry(
        total=5,
        connect=3,
        read=3,
        status=5,
        backoff_factor=2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def _time_period(fy: int, days_back: int | None) -> list[dict]:
    fy_start, fy_end = fiscal_year_bounds(fy)
    today = date.today()
    if days_back is None:
        return [
            {
                "start_date": fy_start.isoformat(),
                "end_date": min(fy_end, today).isoformat(),
                "date_type": "action_date",
            }
        ]
    window_start = today - timedelta(days=days_back)
    start = max(fy_start, window_start)
    end = min(fy_end, today)
    if start > end:
        start = end
    return [
        {
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "date_type": "last_modified_date",
        }
    ]


def search_awards(
    session: requests.Session,
    award_type: str,
    agency_name: str,
    fy: int,
    days_back: int | None,
    remaining: int | None,
) -> list[dict]:
    group = AWARD_GROUPS[award_type]
    collected: list[dict] = []
    page = 1
    while True:
        if remaining is not None and remaining <= 0:
            break
        limit = PAGE_SIZE
        if remaining is not None:
            limit = min(PAGE_SIZE, remaining)
        body = {
            "subawards": False,
            "limit": limit,
            "page": page,
            "filters": {
                "award_type_codes": group["codes"],
                "time_period": _time_period(fy, days_back),
                "agencies": [
                    {"type": "awarding", "tier": "toptier", "name": agency_name}
                ],
            },
            "fields": group["fields"],
        }
        logger.info(
            "USAspending search type=%s agency=%s fy=%s page=%s limit=%s",
            award_type,
            agency_name,
            fy,
            page,
            limit,
        )
        resp = session.post(
            USASPENDING_SEARCH_URL,
            json=body,
            timeout=config.REQUEST_TIMEOUT_SECONDS,
        )
        if resp.status_code != 200:
            logger.error(
                "USAspending HTTP %s: %s", resp.status_code, resp.text[:400]
            )
            resp.raise_for_status()
        data = resp.json()
        rows = data.get("results") or []
        collected.extend(rows)
        if remaining is not None:
            remaining -= len(rows)
        has_next = bool((data.get("page_metadata") or {}).get("hasNext"))
        if not rows or not has_next:
            break
        page += 1
        time.sleep(SEARCH_DELAY_SECONDS)
    return collected


def run(
    fiscal_year: int,
    agency: str | None = None,
    days_back: int | None = None,
    max_records: int | None = None,
    dry_run: bool = False,
    client=None,
) -> dict:
    catalog = load_agency_catalog()
    agencies = resolve_agencies(catalog, agency_flag=agency)
    session = _session()
    remaining = max_records
    mapped: list[dict] = []
    review: list[dict] = []
    found = 0

    for ag in agencies:
        if remaining is not None and remaining <= 0:
            break
        for award_type in AWARD_GROUPS:
            if remaining is not None and remaining <= 0:
                break
            rows = search_awards(
                session,
                award_type=award_type,
                agency_name=ag["name"],
                fy=fiscal_year,
                days_back=days_back,
                remaining=remaining,
            )
            found += len(rows)
            if remaining is not None:
                remaining -= len(rows)
            for row in rows:
                rec = map_award(row, award_type, fiscal_year)
                if rec.get("_review"):
                    review.append(rec)
                else:
                    mapped.append(rec)
            time.sleep(SEARCH_DELAY_SECONDS)

    logger.info(
        "usaspending mapped=%s review=%s found=%s fy=%s agencies=%s",
        len(mapped),
        len(review),
        found,
        fiscal_year,
        [a["abbreviation"] for a in agencies],
    )

    posted = 0
    flagged = 0
    if dry_run:
        for rec in mapped[:3]:
            logger.info("dry-run award %s", json.dumps(rec, default=str)[:500])
        for rec in review[:3]:
            logger.info("dry-run review %s", json.dumps(rec, default=str)[:500])
    else:
        hz = client or get_client()
        if not hz.configured:
            raise RuntimeError("HORIZONS_BASE_URL and INGEST_API_KEY required")
        for i in range(0, len(mapped), HORIZONS_BATCH_SIZE):
            batch = mapped[i : i + HORIZONS_BATCH_SIZE]
            hz.submit_spending_awards(batch)
            posted += len(batch)
        for rec in review:
            hz.flag_manual_review(
                filing_id=rec.get("award_id") or "unknown",
                reason=rec.get("reason") or "unexpected_structure",
                notes=rec.get("notes") or "",
            )
            flagged += 1
        hz.submit_ingestion_log(
            source="usaspending",
            found=found,
            parsed=len(mapped),
            failed=0,
            flagged=len(review),
            note=f"fy={fiscal_year} agencies={[a['abbreviation'] for a in agencies]}",
        )

    return {
        "found": found,
        "mapped": len(mapped),
        "review": len(review),
        "posted": posted,
        "flagged": flagged,
        "fiscal_year": fiscal_year,
        "agencies": [a["abbreviation"] for a in agencies],
        "dry_run": dry_run,
    }


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="USAspending award ingest")
    p.add_argument("--fiscal-year", type=int, required=True, help="Federal FY (e.g. 2026)")
    p.add_argument(
        "--agency",
        default=None,
        help="Single agency abbreviation or full name (default: allowlist)",
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
        config.require_config(dry_run=args.dry_run, source="usaspending")
    except RuntimeError as e:
        logger.error(str(e))
        return 1
    if args.dry_run:
        logger.info("Dry run — no Horizons writes")
    result = run(
        fiscal_year=args.fiscal_year,
        agency=args.agency,
        days_back=args.days_back,
        max_records=args.max_records,
        dry_run=args.dry_run,
    )
    logger.info("=== usaspending summary === %s", result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
