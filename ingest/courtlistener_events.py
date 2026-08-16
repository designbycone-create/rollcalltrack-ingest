from __future__ import annotations

from typing import Any

from .judicial_events import classify_opinion_text, event_id
from .judicial_score import WEIGHTS

CL_ORIGIN = "https://www.courtlistener.com"


def match_judge(name: str, court: str, roster: list[dict]) -> str | None:
    target = (name or "").strip().casefold()
    court_q = (court or "").strip().casefold()
    if not target:
        return None
    hits = []
    for row in roster:
        full = (row.get("full_name") or "").strip().casefold()
        row_court = (row.get("court") or "").strip().casefold()
        if full != target:
            continue
        if court_q and court_q not in row_court:
            continue
        hits.append(row.get("judge_id"))
    if len(hits) == 1:
        return hits[0]
    return None


def _source_url(absolute_url: str) -> str:
    url = (absolute_url or "").strip()
    if url.startswith("http://") or url.startswith("https://"):
        return url
    if not url.startswith("/"):
        url = "/" + url
    return CL_ORIGIN + url


def map_cluster(cluster: dict[str, Any], roster: list[dict]) -> dict[str, Any]:
    classified = classify_opinion_text(cluster.get("plain_text") or "")
    if classified["status"] != "write":
        return {
            "status": "review",
            "event": None,
            "reason": classified.get("reason") or "no scored event pattern",
        }
    judge_id = match_judge(
        cluster.get("judges") or "",
        cluster.get("court") or "",
        roster,
    )
    if not judge_id:
        return {
            "status": "review",
            "event": None,
            "reason": "unmatched_judge",
        }
    et = classified["event_type"]
    url = _source_url(cluster.get("absolute_url") or "")
    return {
        "status": "write",
        "event": {
            "event_id": event_id(url, et),
            "judge_id": judge_id,
            "event_type": et,
            "disposition": classified.get("disposition") or "n/a",
            "event_date": (cluster.get("date_filed") or "")[:10],
            "court": cluster.get("court") or "",
            "case_caption": cluster.get("case_name") or "",
            "source_url": url,
            "classification": classified["classification"],
            "weight": WEIGHTS[et],
        },
    }


def run(
    *,
    dry_run: bool = True,
    days_back: int | None = None,
    max_records: int | None = None,
    clusters: list[dict] | None = None,
    roster: list[dict] | None = None,
    client=None,
) -> dict:
    items = list(clusters or [])
    if max_records is not None:
        items = items[:max_records]
    mapped: list[dict] = []
    review: list[dict] = []
    for cluster in items:
        out = map_cluster(cluster, roster or [])
        if out["status"] == "write" and out.get("event"):
            mapped.append(out["event"])
        else:
            review.append(out)
    posted = 0
    if not dry_run and mapped:
        if client is None:
            from .ingest_client import get_client

            client = get_client()
        for i in range(0, len(mapped), 25):
            client.submit_judicial_events(mapped[i : i + 25])
            posted += len(mapped[i : i + 25])
    return {
        "found": len(items),
        "mapped": len(mapped),
        "review": len(review),
        "posted": posted,
        "dry_run": dry_run,
        "days_back": days_back,
    }


def main(argv: list[str] | None = None) -> int:
    import argparse
    import logging
    import sys

    from . import config

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    p = argparse.ArgumentParser(description="CourtListener judicial event ingest")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--days-back", type=int, default=14)
    p.add_argument("--max-records", type=int, default=None)
    args = p.parse_args(argv)
    config.require_config(dry_run=args.dry_run, source="fjc")
    result = run(
        dry_run=args.dry_run,
        days_back=args.days_back,
        max_records=args.max_records,
    )
    logging.getLogger("rollcalltrack_ingest.courtlistener_events").info(
        "=== courtlistener-events summary === %s", result
    )
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
