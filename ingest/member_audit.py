"""Read-only check for state legislators stored as federal members.

LegiScan rows use ``LS{people_id}`` and ``jurisdiction=state``. Congressional
bioguides are one letter plus six digits and belong to the Horizons
Congress.gov member sync, which is not this repo.

This module never deletes or POSTs. ``python -m ingest.member_audit``
prints rows an operator can retire by hand.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from typing import Any

import requests

from . import config
from .legislators import load_current_legislators, match_legislator

logger = logging.getLogger("rollcalltrack_ingest.member_audit")

# LegiScan people ids are numeric. LS23231 is a state row.
# W000832 is a congressional bioguide and must not be written by this repo.
STATE_MEMBER_ID_RE = re.compile(r"^LS[0-9]+$")

PUBLIC_SITE = "https://rollcalltrack.com"


def is_state_member_id(value: str | None) -> bool:
    return bool(STATE_MEMBER_ID_RE.match((value or "").strip()))


def assert_state_member_record(rec: dict) -> None:
    """Reject a members payload that could land in the federal roster."""
    if rec.get("jurisdiction") != "state":
        raise ValueError("this worker only posts jurisdiction=state")
    bio = str(rec.get("bioguide_id") or "")
    if not is_state_member_id(bio):
        raise ValueError(
            "state member bioguide_id must be LS{people_id}; "
            f"refusing congressional-style id {bio!r}"
        )


def classify_members(
    federal_rows: list[dict],
    state_rows: list[dict],
    official_roster: list[dict],
) -> dict[str, Any]:
    """Split live member rows into keep vs review.

    ``federal_not_official`` would be state legislators (or anyone else)
    sitting in the federal table. ``state_overlap`` is a state seat whose
    name and state match someone already in Congress. ``state_bad_id`` is
    a state row whose id is not ``LS{digits}``.
    """
    official_ids = {
        (m.get("bioguide") or "").strip()
        for m in official_roster
        if m.get("bioguide")
    }
    federal_not_official = []
    for row in federal_rows:
        bio = str(row.get("bioguide_id") or row.get("id") or "").strip()
        if bio in official_ids:
            continue
        federal_not_official.append(
            {
                "id": row.get("id"),
                "bioguide_id": bio,
                "name": row.get("name"),
                "state": row.get("state") or row.get("state_code"),
                "chamber": row.get("chamber"),
                "jurisdiction": row.get("jurisdiction"),
                "source_url": row.get("source_url"),
            }
        )
    state_bad_id = []
    state_overlap = []
    for row in state_rows:
        bio = str(row.get("bioguide_id") or "").strip()
        if not is_state_member_id(bio):
            state_bad_id.append(
                {
                    "id": row.get("id"),
                    "bioguide_id": bio,
                    "name": row.get("name"),
                    "state_code": row.get("state_code") or row.get("state"),
                    "chamber": row.get("chamber"),
                    "jurisdiction": row.get("jurisdiction"),
                    "source_url": row.get("source_url"),
                }
            )
        hit = match_legislator(
            row.get("name") or "",
            official_roster,
            state=row.get("state_code") or row.get("state"),
        )
        if hit:
            state_overlap.append(
                {
                    "id": row.get("id"),
                    "bioguide_id": bio,
                    "name": row.get("name"),
                    "state_code": row.get("state_code") or row.get("state"),
                    "chamber": row.get("chamber"),
                    "district": row.get("district"),
                    "source_url": row.get("source_url"),
                    "federal_bioguide": hit.get("bioguide"),
                    "federal_name": hit.get("official_full"),
                }
            )
    return {
        "federal_count": len(federal_rows),
        "state_count": len(state_rows),
        "official_count": len(official_ids),
        "federal_not_official": federal_not_official,
        "state_bad_id": state_bad_id,
        "state_overlap": state_overlap,
    }


def cleanup_steps(report: dict) -> str:
    """Operator instructions. Nothing here deletes a row."""
    lines = [
        "Read-only report. This command does not delete or POST.",
        "",
        "Federal members whose bioguide is in legislators-current.json are",
        "current members of Congress. Leave them. Aisha Wahab (W000832,",
        "California 14, sworn 2026-09-02) and Everton Blair Jr. (B001328,",
        "Georgia 13, sworn 2026-09-01) are in that official roster. Their",
        "House rows were not written by legiscan-legislators.",
        "",
        "Rows under federal_not_official are in the federal members table",
        "without an official current bioguide. Retire those in the Horizons",
        "members admin after confirming the id is absent from",
        "https://unitedstates.github.io/congress-legislators/legislators-current.json.",
        f"Count this run: {len(report.get('federal_not_official') or [])}.",
        "",
        "Rows under state_bad_id use an id other than LS{people_id}.",
        "This repo refuses to post those. Retire any already stored.",
        f"Count this run: {len(report.get('state_bad_id') or [])}.",
        "",
        "Rows under state_overlap share a name and state with a current",
        "member of Congress. That is a review list, not a delete list.",
        "The same name can be two people: Louisiana House District 27",
        "Mike Johnson (LS21343) is not Speaker Mike Johnson (J000299).",
        "Retire a state row only after checking the LegiScan source_url",
        "and confirming that person left the legislature. Do not delete",
        "the federal bioguide. Aisha Wahab's state seat is LS23231",
        "(California Senate district 10). Everton Blair has no state row.",
        f"Count this run: {len(report.get('state_overlap') or [])}.",
        "",
        "There is no delete route in this repo. Do not run a production delete from ingest.",
    ]
    return "\n".join(lines)


def _base_url() -> str:
    return (config.HORIZONS_BASE_URL or PUBLIC_SITE).rstrip("/")


def fetch_member_pages(
    session: requests.Session,
    *,
    jurisdiction: str,
    base_url: str | None = None,
) -> list[dict]:
    base = (base_url or _base_url()).rstrip("/")
    page = 1
    out: list[dict] = []
    while True:
        resp = session.get(
            f"{base}/hcgi/api/members",
            params={"jurisdiction": jurisdiction, "limit": 100, "page": page},
            timeout=config.REQUEST_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        data = resp.json()
        items = data.get("items") or []
        out.extend(items)
        total_pages = int(data.get("totalPages") or 1)
        if page >= total_pages or not items:
            break
        page += 1
    return out


def build_report(
    session: requests.Session | None = None,
    *,
    base_url: str | None = None,
    official_roster: list[dict] | None = None,
) -> dict[str, Any]:
    sess = session or requests.Session()
    sess.headers.setdefault("User-Agent", config.USER_AGENT)
    roster = official_roster if official_roster is not None else load_current_legislators(sess)
    federal = fetch_member_pages(sess, jurisdiction="federal", base_url=base_url)
    state = fetch_member_pages(sess, jurisdiction="state", base_url=base_url)
    report = classify_members(federal, state, roster)
    report["cleanup"] = cleanup_steps(report)
    return report


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(
        description="Report federal-member rows that are not current members of Congress"
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="Site origin. Defaults to HORIZONS_BASE_URL or https://rollcalltrack.com",
    )
    args = parser.parse_args(argv)
    try:
        report = build_report(base_url=args.base_url)
    except (requests.RequestException, ValueError, TypeError) as e:
        logger.error("member audit failed: %s", e)
        return 1
    text = {
        "federal_count": report["federal_count"],
        "state_count": report["state_count"],
        "official_count": report["official_count"],
        "federal_not_official": report["federal_not_official"],
        "state_bad_id": report["state_bad_id"],
        "state_overlap": report["state_overlap"],
    }
    print(json.dumps(text, indent=2))
    print()
    print(report["cleanup"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
