from __future__ import annotations

import logging
import re
from typing import Any

from .courtlistener_events import fetch_opinion_hits, match_judge
from .judicial_events import event_id
from .judicial_score import WEIGHTS

logger = logging.getLogger("rollcalltrack_ingest.scotus_stays")

_DISPOSITION_TO_TYPE = {
    "stayed": "scotus_stay",
    "reversed": "scotus_reversed",
    "vacated": "scotus_reversed",
}

_JUDGE_IN_TEXT = re.compile(
    r"(?:Judge|Justice|District Judge)\s+([A-Z][A-Za-z'.-]+(?:\s+[A-Z][A-Za-z'.-]+){0,3})",
)


def disposition_from_text(text: str) -> str | None:
    t = (text or "").casefold()
    if "application for stay" in t and "granted" in t:
        return "stayed"
    if re.search(r"\bstay(?:ed)?\b", t) and "granted" in t:
        return "stayed"
    if "reversed" in t:
        return "reversed"
    if "vacated" in t:
        return "vacated"
    return None


def cluster_to_order(cluster: dict[str, Any]) -> dict[str, Any]:
    text = cluster.get("plain_text") or ""
    disp = disposition_from_text(text)
    m = _JUDGE_IN_TEXT.search(text)
    lower = cluster.get("judges") or ""
    if m:
        lower = m.group(1)
    raw_url = cluster.get("absolute_url") or ""
    if raw_url.startswith("/"):
        raw_url = "https://www.courtlistener.com" + raw_url
    return {
        "source_url": raw_url,
        "date_filed": cluster.get("date_filed") or "",
        "case_name": cluster.get("case_name") or "",
        "lower_court": cluster.get("court") or "Supreme Court of the United States",
        "lower_judge": lower,
        "disposition": disp or "other",
        "plain_text": text,
    }


def map_scotus_order(order: dict[str, Any], roster: list[dict]) -> dict[str, Any]:
    disp = (order.get("disposition") or "").strip().lower()
    event_type = _DISPOSITION_TO_TYPE.get(disp)
    if not event_type:
        return {"status": "review", "event": None, "reason": "unmapped_disposition"}
    name = order.get("lower_judge") or ""
    court = order.get("lower_court") or ""
    judge_id = match_judge(name, court, roster) or match_judge(name, "", roster)
    if not judge_id:
        return {"status": "review", "event": None, "reason": "unmatched_judge"}
    url = (order.get("source_url") or "").strip()
    return {
        "status": "write",
        "event": {
            "event_id": event_id(url, event_type),
            "judge_id": judge_id,
            "event_type": event_type,
            "disposition": disp,
            "event_date": (order.get("date_filed") or "")[:10],
            "court": court or "Supreme Court of the United States",
            "case_caption": order.get("case_name") or "",
            "source_url": url,
            "classification": "higher_court_disposition",
            "weight": WEIGHTS[event_type],
        },
    }


def run(
    *,
    dry_run: bool = True,
    days_back: int | None = None,
    max_records: int | None = None,
    orders: list[dict] | None = None,
    roster: list[dict] | None = None,
    client=None,
) -> dict:
    if roster is None:
        from .ingest_client import get_client

        hz = client or get_client()
        try:
            roster = hz.fetch_judicial_judges()
        except Exception as exc:
            logger.warning("could not load judicial roster: %s", exc)
            roster = []
    if orders is None:
        hits = fetch_opinion_hits(
            '"application for stay" OR "stay granted" OR reversed OR vacated',
            days_back=days_back or 14,
            max_records=max_records,
            court="scotus",
        )
        orders = [cluster_to_order(h) for h in hits]
    items = list(orders)
    if max_records is not None:
        items = items[:max_records]
    mapped: list[dict] = []
    review: list[dict] = []
    for order in items:
        out = map_scotus_order(order, roster or [])
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
    p = argparse.ArgumentParser(description="SCOTUS stay/reversal event ingest")
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
    logging.getLogger("rollcalltrack_ingest.scotus_stays").info(
        "=== scotus-stays summary === %s", result
    )
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
