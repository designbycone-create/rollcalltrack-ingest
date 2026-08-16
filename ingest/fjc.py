"""FJC Article III Biographical Directory ingest (fjc-judges-sync).

Official nightly flat-file export:
  https://www.fjc.gov/sites/default/files/history/judges.csv

This is a low-frequency full-refresh job. There is no incremental window —
every run downloads the whole directory. ``--full-refresh`` is accepted and
is the only mode.

judge_id is FJC's ``nid`` (Node ID), a unique identifier FJC generates for
the biographical database. The same nid is the path of the official bio
page: https://www.fjc.gov/node/{nid}. ``jid`` is a pre-2016 leftover and
is not assigned to newer judges — do not use it.

Bankruptcy and magistrate posts appear only under Other Federal Judicial
Service. This file is Article III service; those enums stay in the
contract but will almost never fire from this source.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import sys
import time
from datetime import datetime
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import config
from .ingest_client import get_client

logger = logging.getLogger("rollcalltrack_ingest.fjc")

FJC_EXPORT_PAGE = (
    "https://www.fjc.gov/history/judges/"
    "biographical-directory-article-iii-federal-judges-export"
)
FJC_CSV_URL = "https://www.fjc.gov/sites/default/files/history/judges.csv"
FJC_BIO_URL = "https://www.fjc.gov/node/{nid}"

HORIZONS_BATCH_SIZE = 25
SERVICE_SLOTS = range(1, 7)

COURT_TYPE_ENUM = (
    "supreme_court",
    "circuit_court",
    "district_court",
    "bankruptcy_court",
    "magistrate",
    "other",
)
STATUS_ENUM = (
    "active",
    "senior_status",
    "retired",
    "resigned",
    "deceased",
)

# Last-slot values that mean the judge left office, mapped 1:1.
_RESIGNED_TERMINATIONS = frozenset(
    {
        "resignation",
        "impeachment & conviction",
    }
)
_RETIRED_TERMINATIONS = frozenset({"retirement"})
# Historical exits that are not in the status enum. Review unless deceased.
_REVIEW_TERMINATIONS = frozenset(
    {
        "recess appointment-not confirmed",
        "abolition of court",
    }
)


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


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def iso_date(value: Any) -> str | None:
    """FJC docs say CSV dates are mm/dd/yyyy; the live file uses YYYY-MM-DD."""
    s = clean_text(value)
    if not s:
        return None
    if "T" in s:
        s = s.split("T", 1)[0]
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def source_url_for(nid: str) -> str:
    return FJC_BIO_URL.format(nid=nid)


def full_name(row: dict) -> str | None:
    parts = []
    for key in ("First Name", "Middle Name", "Last Name"):
        part = clean_text(row.get(key))
        if part:
            parts.append(part)
    name = " ".join(parts)
    suffix = clean_text(row.get("Suffix"))
    if suffix:
        name = f"{name} {suffix}".strip()
    return name or None


def iter_services(row: dict) -> list[dict]:
    """Article III service slots 1–6, in FJC order (1 = earliest)."""
    slots: list[dict] = []
    for i in SERVICE_SLOTS:
        court = clean_text(row.get(f"Court Name ({i})"))
        court_type = clean_text(row.get(f"Court Type ({i})"))
        if not court and not court_type:
            continue
        slots.append(
            {
                "index": i,
                "court": court,
                "court_type_raw": court_type,
                "appointing_president": clean_text(
                    row.get(f"Appointing President ({i})")
                ),
                "aba_rating": clean_text(row.get(f"ABA Rating ({i})")),
                "nomination_date": iso_date(row.get(f"Nomination Date ({i})")),
                "confirmation_date": iso_date(row.get(f"Confirmation Date ({i})")),
                "senior_status_date": iso_date(row.get(f"Senior Status Date ({i})")),
                "termination": clean_text(row.get(f"Termination ({i})")),
            }
        )
    return slots


def map_court_type(court_type_raw: str | None, court_name: str | None) -> str:
    ct = (court_type_raw or "").lower()
    name = (court_name or "").lower()
    if "supreme" in ct or "supreme court of the united states" in name:
        return "supreme_court"
    if "court of appeals" in ct or "circuit court" in ct:
        return "circuit_court"
    if "district court" in ct:
        return "district_court"
    if "international trade" in name or name == "u.s. customs court":
        return "district_court"
    if "customs and patent appeals" in name or "customs appeals" in name:
        return "circuit_court"
    if "bankruptcy" in ct or "bankruptcy" in name:
        return "bankruptcy_court"
    if "magistrate" in ct or "magistrate" in name:
        return "magistrate"
    return "other"


def map_status(death_year: str | None, latest: dict) -> str | None:
    """Return a status enum, or None if the last-slot exit is not mappable."""
    if death_year:
        return "deceased"
    term = (latest.get("termination") or "").lower()
    if term == "death":
        return "deceased"
    if term in _RESIGNED_TERMINATIONS:
        return "resigned"
    if term in _RETIRED_TERMINATIONS:
        return "retired"
    if term in _REVIEW_TERMINATIONS or term:
        return None
    if latest.get("senior_status_date"):
        return "senior_status"
    return "active"


def map_row(row: dict) -> dict:
    nid = clean_text(row.get("nid"))
    name = full_name(row)
    slots = iter_services(row)
    if not nid:
        return {
            "_review": True,
            "reason": "unexpected_structure",
            "notes": "missing FJC nid",
            "full_name": name,
        }
    if not name:
        return {
            "_review": True,
            "reason": "unexpected_structure",
            "notes": f"nid={nid} missing name",
            "judge_id": nid,
        }
    if not slots:
        return {
            "_review": True,
            "reason": "unexpected_structure",
            "notes": f"nid={nid} has no Article III court slot",
            "judge_id": nid,
            "full_name": name,
            "source_url": source_url_for(nid),
        }

    latest = slots[-1]
    court = latest.get("court")
    if not court:
        return {
            "_review": True,
            "reason": "unexpected_structure",
            "notes": f"nid={nid} latest slot has no Court Name",
            "judge_id": nid,
            "full_name": name,
            "source_url": source_url_for(nid),
        }

    death_year = clean_text(row.get("Death Year"))
    status = map_status(death_year, latest)
    if status is None:
        return {
            "_review": True,
            "reason": "unexpected_structure",
            "notes": (
                f"nid={nid} last-slot Termination="
                f"{latest.get('termination')!r} is not in the status enum"
            ),
            "judge_id": nid,
            "full_name": name,
            "court": court,
            "source_url": source_url_for(nid),
        }

    return {
        "judge_id": nid,
        "full_name": name,
        "court": court,
        "court_type": map_court_type(latest.get("court_type_raw"), court),
        "appointing_president": latest.get("appointing_president"),
        "nomination_date": latest.get("nomination_date"),
        "confirmation_date": latest.get("confirmation_date"),
        "aba_rating": latest.get("aba_rating"),
        "status": status,
        "source_url": source_url_for(nid),
    }


def parse_csv(text: str, max_records: int | None = None) -> list[dict]:
    reader = csv.DictReader(io.StringIO(text))
    rows = []
    for row in reader:
        rows.append(row)
        if max_records is not None and len(rows) >= max_records:
            break
    return rows


def download_csv(session: requests.Session | None = None) -> str:
    s = session or _session()
    logger.info("Downloading FJC judges.csv %s", FJC_CSV_URL)
    resp = s.get(FJC_CSV_URL, timeout=config.REQUEST_TIMEOUT_SECONDS)
    resp.raise_for_status()
    # utf-8-sig strips a BOM if FJC adds one.
    return resp.content.decode("utf-8-sig")


def run(
    dry_run: bool = False,
    full_refresh: bool = True,
    max_records: int | None = None,
    client=None,
    csv_text: str | None = None,
) -> dict:
    """Map the FJC directory and optionally POST it.

    ``full_refresh`` is always true in practice — this job has no
    incremental path. The flag exists so callers can be explicit.
    """
    if not full_refresh:
        logger.info(
            "This job is a full directory refresh; ignoring incremental intent"
        )
    text = csv_text if csv_text is not None else download_csv()
    rows = parse_csv(text, max_records=max_records)
    mapped: list[dict] = []
    review: list[dict] = []
    for row in rows:
        rec = map_row(row)
        if rec.get("_review"):
            review.append(rec)
        else:
            mapped.append(rec)

    logger.info(
        "fjc found=%s mapped=%s review=%s full_refresh=%s",
        len(rows),
        len(mapped),
        len(review),
        True,
    )

    posted = 0
    if dry_run:
        for rec in mapped[:5]:
            logger.info("dry-run judge %s", json.dumps(rec, default=str)[:500])
        for rec in review[:5]:
            logger.info("dry-run review %s", json.dumps(rec, default=str)[:500])
    else:
        hz = client or get_client()
        if not hz.configured:
            raise RuntimeError("HORIZONS_BASE_URL and INGEST_API_KEY required")
        for i in range(0, len(mapped), HORIZONS_BATCH_SIZE):
            batch = mapped[i : i + HORIZONS_BATCH_SIZE]
            hz.submit_judicial_judges(batch)
            posted += len(batch)
            if i + HORIZONS_BATCH_SIZE < len(mapped):
                time.sleep(0.4)
        for rec in review:
            hz.flag_manual_review(
                filing_id=rec.get("judge_id") or rec.get("source_url") or "unknown",
                reason=rec.get("reason") or "unexpected_structure",
                notes=rec.get("notes") or "",
            )
        hz.submit_ingestion_log(
            source="fjc",
            found=len(rows),
            parsed=len(mapped),
            failed=len(review),
            flagged=len(review),
            note="full-refresh FJC Article III directory",
        )

    return {
        "found": len(rows),
        "mapped": len(mapped),
        "review": len(review),
        "posted": posted,
        "dry_run": dry_run,
        "full_refresh": True,
    }


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="FJC Article III judges ingest (full-refresh)"
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and log; do not POST",
    )
    p.add_argument(
        "--full-refresh",
        action="store_true",
        help="Download the full FJC directory (this is the only mode)",
    )
    p.add_argument(
        "--max-records",
        type=int,
        default=None,
        help="Cap rows (testing only)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = build_arg_parser().parse_args(argv)
    try:
        config.require_config(dry_run=args.dry_run, source="fjc")
    except RuntimeError as e:
        logger.error(str(e))
        return 1
    result = run(
        dry_run=args.dry_run,
        full_refresh=True,
        max_records=args.max_records,
    )
    logger.info("=== fjc summary === %s", result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
