from __future__ import annotations

import logging
import os
import re
from datetime import date, timedelta
from typing import Any

import requests

from .judicial_events import classify_opinion_text, event_id
from .judicial_score import WEIGHTS

logger = logging.getLogger("rollcalltrack_ingest.courtlistener_events")

CL_ORIGIN = "https://www.courtlistener.com"
CL_SEARCH = f"{CL_ORIGIN}/api/rest/v4/search/"

_TITLE_RE = re.compile(
    r"^(?:hon(?:orable)?\.?|chief\s+judge|judge|justice|j\.)\s+",
    re.I,
)


def clean_judge_name(name: str) -> str:
    s = (name or "").strip()
    s = _TITLE_RE.sub("", s)
    s = re.sub(r"\s+", " ", s).strip(" ,")
    return s


def match_judge(name: str, court: str, roster: list[dict]) -> str | None:
    target = clean_judge_name(name).casefold()
    court_q = (court or "").strip().casefold()
    if not target:
        return None

    def last(full: str) -> str:
        parts = [p for p in re.split(r"[\s,]+", full) if p]
        return parts[-1] if parts else ""

    exact = []
    last_hits = []
    for row in roster:
        full = clean_judge_name(row.get("full_name") or "").casefold()
        row_court = (row.get("court") or "").strip().casefold()
        jid = row.get("judge_id")
        if full == target:
            if not court_q or court_q in row_court:
                exact.append(jid)
        if last(full) and last(full) == last(target):
            last_hits.append(jid)
    if len(exact) == 1:
        return exact[0]
    uniq_last = list(dict.fromkeys(last_hits))
    if len(uniq_last) == 1:
        return uniq_last[0]
    return None


def search_hit_to_cluster(hit: dict[str, Any]) -> dict[str, Any]:
    opinions = hit.get("opinions") or []
    snippet = ""
    if opinions and isinstance(opinions[0], dict):
        snippet = opinions[0].get("snippet") or ""
    judge = hit.get("judge") or ""
    if not judge:
        names = hit.get("panel_names") or []
        if isinstance(names, list) and names:
            judge = names[0]
        elif isinstance(names, str):
            judge = names
    return {
        "absolute_url": hit.get("absolute_url") or "",
        "date_filed": (hit.get("dateFiled") or "")[:10],
        "case_name": hit.get("caseName") or "",
        "court": hit.get("court") or hit.get("court_citation_string") or "",
        "court_id": hit.get("court_id") or "",
        "judges": judge,
        "plain_text": snippet,
    }


def fetch_opinion_hits(
    query: str,
    *,
    days_back: int = 14,
    max_records: int | None = None,
    court: str | None = None,
    session: requests.Session | None = None,
) -> list[dict[str, Any]]:
    s = session or requests.Session()
    s.headers.setdefault(
        "User-Agent",
        "rollcalltrack.com research bot (contact: hello@rollcalltrack.com)",
    )
    token = os.environ.get("COURTLISTENER_TOKEN") or ""
    if token:
        s.headers["Authorization"] = f"Token {token}"
    start = (date.today() - timedelta(days=days_back or 14)).isoformat()
    q = f"({query}) AND dateFiled:[{start} TO *]"
    params: dict[str, Any] = {
        "q": q,
        "type": "o",
        "order_by": "dateFiled desc",
    }
    if court:
        params["court"] = court
    cap = max_records if max_records is not None else 50
    out: list[dict[str, Any]] = []
    url = CL_SEARCH
    while url and len(out) < cap:
        resp = s.get(url, params=params if url == CL_SEARCH else None, timeout=45)
        params = None
        if resp.status_code != 200:
            logger.error("CourtListener search HTTP %s: %s", resp.status_code, resp.text[:200])
            break
        payload = resp.json()
        for hit in payload.get("results") or []:
            out.append(search_hit_to_cluster(hit))
            if len(out) >= cap:
                break
        url = payload.get("next")
    return out


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
    if roster is None:
        from .ingest_client import get_client

        hz = client or get_client()
        try:
            roster = hz.fetch_judicial_judges()
        except Exception as exc:
            logger.warning("could not load judicial roster: %s", exc)
            roster = []
    if clusters is None:
        want = max_records if max_records is not None else 50
        raw = fetch_opinion_hits(
            '"nationwide injunction" OR "universal injunction"',
            days_back=days_back or 14,
            max_records=max(want * 10, 80),
        )
        clusters = [
            c
            for c in raw
            if (c.get("court_id") or "").startswith("d")
            or "district" in (c.get("court") or "").casefold()
        ]
        clusters = clusters[:want]
    items = list(clusters)
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
