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
