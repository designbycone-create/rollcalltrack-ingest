from __future__ import annotations

from typing import Any

from .courtlistener_events import match_judge
from .judicial_events import event_id
from .judicial_score import WEIGHTS

_DISPOSITION_TO_TYPE = {
    "stayed": "scotus_stay",
    "reversed": "scotus_reversed",
    "vacated": "scotus_reversed",
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
