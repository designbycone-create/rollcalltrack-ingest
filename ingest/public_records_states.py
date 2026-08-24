"""50-state + federal public-records jurisdiction catalog.

Reads ingest/data/state_public_records.json. Does not invent emails
or response_days. Null is valid. Submit stays on the official portal;
we do not file requests.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from urllib.parse import urlparse

from .ingest_client import get_client
from .legiscan import STATE_CODES

logger = logging.getLogger("rollcalltrack_ingest.public_records_states")

CATALOG_PATH = Path(__file__).resolve().parent / "data" / "state_public_records.json"
HORIZONS_BATCH_SIZE = 25
OFFICIAL_SUFFIXES = (".gov", ".mil", ".us")


def is_official_host(url: str) -> bool:
    host = (urlparse(url or "").hostname or "").lower().rstrip(".")
    if not host:
        return False
    return any(host.endswith(sfx) for sfx in OFFICIAL_SUFFIXES)


def load_catalog(path: Path | None = None) -> list[dict]:
    p = path or CATALOG_PATH
    with p.open(encoding="utf-8") as f:
        data = json.load(f)
    rows = data.get("jurisdictions") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise ValueError(
            "state_public_records.json must be a list or {jurisdictions: []}"
        )
    return rows


def validate_row(row: dict) -> dict:
    if not isinstance(row, dict):
        raise ValueError("row must be an object")
    src = (row.get("source_url") or "").strip()
    portal = (row.get("portal_url") or "").strip()
    if not src:
        raise ValueError("source_url is required")
    if not portal:
        raise ValueError("portal_url is required")
    if not src.startswith("http"):
        raise ValueError("source_url must be http(s)")
    if not portal.startswith("http"):
        raise ValueError("portal_url must be http(s)")
    if not is_official_host(src):
        raise ValueError(f"source_url host is not official: {src}")
    jur = row.get("jurisdiction")
    if jur not in ("federal", "state"):
        raise ValueError("invalid jurisdiction")
    name = (row.get("name") or "").strip()
    if not name:
        raise ValueError("name is required")
    out = dict(row)
    out["source_url"] = src
    out["portal_url"] = portal
    out["name"] = name
    out["jurisdiction"] = jur
    if jur == "federal":
        out["state_code"] = None
    else:
        code = (row.get("state_code") or "").strip().upper()
        if code not in STATE_CODES:
            raise ValueError("state_code is required for state rows")
        out["state_code"] = code
    return out


def run(*, dry_run: bool = False, client=None) -> dict:
    raw = load_catalog()
    rows = [validate_row(r) for r in raw]
    posted = 0
    if dry_run:
        for rec in rows[:5]:
            logger.info("dry-run jurisdiction %s", json.dumps(rec, default=str)[:400])
    else:
        hz = client or get_client()
        if not hz.configured:
            raise RuntimeError("HORIZONS_BASE_URL and INGEST_API_KEY required")
        for i in range(0, len(rows), HORIZONS_BATCH_SIZE):
            batch = rows[i : i + HORIZONS_BATCH_SIZE]
            hz.submit_public_records_jurisdictions(batch)
            posted += len(batch)
    summary = {
        "found": len(raw),
        "mapped": len(rows),
        "posted": posted if not dry_run else 0,
        "dry_run": dry_run,
    }
    logger.info("=== public-records states summary === %s", summary)
    return summary


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    p = argparse.ArgumentParser(
        description="Ingest federal + 50-state public-records jurisdictions"
    )
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    run(dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
