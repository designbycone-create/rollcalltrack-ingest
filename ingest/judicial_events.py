from __future__ import annotations

import hashlib
import re

EVENT_TYPES = (
    "scotus_stay",
    "scotus_reversed",
    "nationwide_injunction",
    "vacatur_nonparty",
    "standing_rebuke",
    "recusal_conflict",
    "circuit_reversed",
    "circuit_vacated",
)

CLASSIFICATIONS = (
    "explicit_text",
    "higher_court_disposition",
    "structured_dataset",
)

_NATIONWIDE = re.compile(
    r"nationwide\s+(?:preliminary\s+)?injunction|universal\s+injunction",
    re.I,
)
_VACATUR = re.compile(
    r"\b(?:vacates?|vacatur|sets?\s+aside)\b",
    re.I,
)
_APA = re.compile(r"\bAPA\b|Administrative\s+Procedure\s+Act", re.I)
_NONPARTY = re.compile(
    r"non[- ]parties|nationwide|not limited to (?:the )?plaintiffs",
    re.I,
)
_PLAINTIFFS_ONLY = re.compile(
    r"named\s+plaintiffs\s+only|parties\s+only|as\s+to\s+plaintiffs\s+only",
    re.I,
)
_INJUNCTION = re.compile(r"\binjunction\b", re.I)


def event_id(source_url: str, event_type: str) -> str:
    raw = f"{(source_url or '').strip()}|{event_type}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def classify_opinion_text(text: str) -> dict:
    body = (text or "").strip()
    if not body:
        return {
            "status": "review",
            "event_type": None,
            "classification": None,
            "disposition": None,
            "reason": "empty opinion text",
        }
    if _NATIONWIDE.search(body) and not _PLAINTIFFS_ONLY.search(body):
        return {
            "status": "write",
            "event_type": "nationwide_injunction",
            "classification": "explicit_text",
            "disposition": "granted",
            "reason": "explicit nationwide/universal injunction",
        }
    if _VACATUR.search(body) and _NONPARTY.search(body) and (
        _APA.search(body) or _NATIONWIDE.search(body)
    ):
        return {
            "status": "write",
            "event_type": "vacatur_nonparty",
            "classification": "explicit_text",
            "disposition": "vacated",
            "reason": "APA/nationwide vacatur not limited to parties",
        }
    if _INJUNCTION.search(body):
        return {
            "status": "review",
            "event_type": None,
            "classification": None,
            "disposition": None,
            "reason": "injunction language without non-party bind",
        }
    return {
        "status": "review",
        "event_type": None,
        "classification": None,
        "disposition": None,
        "reason": "no scored event pattern",
    }
