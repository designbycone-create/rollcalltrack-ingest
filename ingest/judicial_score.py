from __future__ import annotations

from datetime import date, datetime
from typing import Any

WEIGHTS: dict[str, int] = {
    "scotus_stay": 5,
    "scotus_reversed": 5,
    "nationwide_injunction": 4,
    "vacatur_nonparty": 4,
    "standing_rebuke": 3,
    "recusal_conflict": 3,
    "circuit_reversed": 2,
    "circuit_vacated": 2,
}


def _as_date(value: str | date) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


def recency_multiplier(event_date: date, today: date) -> float:
    days = (today - event_date).days
    if days < 0:
        days = 0
    years = days / 365.25
    if years < 5:
        return 1.0
    if years < 10:
        return 0.6
    if years < 15:
        return 0.3
    return 0.0


def ideology_label(score: int | None, prior_only: bool = False) -> str | None:
    if prior_only and score is None:
        return "prior_only"
    if score is None:
        return None
    if score <= -30:
        return "liberal"
    if score >= 30:
        return "conservative"
    return "moderate"


def score_judge(
    events: list[dict[str, Any]],
    *,
    today: date,
    ideology_score: int | None = None,
    ideology_prior_only: bool = False,
) -> dict[str, Any]:
    raw = 0.0
    counted = 0
    for ev in events:
        et = ev.get("event_type")
        if et not in WEIGHTS:
            continue
        when = _as_date(ev["event_date"])
        mult = recency_multiplier(when, today)
        if mult <= 0:
            continue
        weight = ev.get("weight") if ev.get("weight") is not None else WEIGHTS[et]
        raw += float(weight) * mult
        counted += 1
    score = min(100, int(round(raw / 20.0 * 100.0))) if counted else 0
    if counted == 0:
        band = "unscored"
    elif score <= 34:
        band = "within_bounds"
    elif score <= 64:
        band = "watch"
    else:
        band = "rogue"
    return {
        "rogue_score": score,
        "rogue_band": band,
        "event_count": counted,
        "raw": raw,
        "ideology_score": ideology_score,
        "ideology_label": ideology_label(ideology_score, ideology_prior_only),
    }


def run(
    *,
    dry_run: bool = True,
    today: date | None = None,
    events_by_judge: dict[str, list[dict[str, Any]]] | None = None,
    snapshot: dict[str, Any] | None = None,
    known_ids: set[str] | None = None,
    client=None,
    max_records: int | None = None,
) -> dict[str, Any]:
    from .jcs_scores import lookup_ideology

    day = today or date.today()
    groups = dict(events_by_judge or {})
    snap = snapshot if snapshot is not None else {"scores": {}}
    scores: list[dict[str, Any]] = []
    review = 0
    judge_ids = list(groups.keys())
    if max_records is not None:
        judge_ids = judge_ids[:max_records]
    for jid in judge_ids:
        if known_ids is not None and jid not in known_ids:
            review += 1
            continue
        ideo = lookup_ideology(snap, jid)
        scored = score_judge(
            groups.get(jid) or [],
            today=day,
            ideology_score=ideo["ideology_score"],
            ideology_prior_only=ideo["ideology_prior_only"],
        )
        row = {
            "judge_id": jid,
            "rogue_score": scored["rogue_score"],
            "rogue_band": scored["rogue_band"],
            "ideology_score": scored["ideology_score"],
            "ideology_label": scored["ideology_label"],
            "event_count": scored["event_count"],
            "scored_through": day.isoformat(),
        }
        scores.append(row)
    posted = 0
    if not dry_run and scores:
        if client is None:
            from .ingest_client import get_client

            client = get_client()
        for i in range(0, len(scores), 25):
            batch = scores[i : i + 25]
            client.submit_judicial_scores(batch)
            posted += len(batch)
    return {
        "found": len(judge_ids),
        "mapped": len(scores),
        "review": review,
        "posted": posted,
        "dry_run": dry_run,
        "scores": scores,
    }


def main(argv: list[str] | None = None) -> int:
    import argparse
    import logging
    import sys

    from . import config
    from .jcs_scores import load_snapshot

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    p = argparse.ArgumentParser(description="Score judges from ingested events")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--max-records", type=int, default=None)
    args = p.parse_args(argv)
    config.require_config(dry_run=args.dry_run, source="fjc")
    result = run(
        dry_run=args.dry_run,
        today=date.today(),
        events_by_judge={},
        snapshot=load_snapshot(),
        max_records=args.max_records,
    )
    logging.getLogger("rollcalltrack_ingest.judicial_score").info(
        "=== judicial-score summary === %s",
        {k: result[k] for k in ("found", "mapped", "review", "posted", "dry_run")},
    )
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
