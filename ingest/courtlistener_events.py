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
