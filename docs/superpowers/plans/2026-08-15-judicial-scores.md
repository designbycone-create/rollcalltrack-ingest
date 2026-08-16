# Judicial Scores Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship GitHub-side event ingest, ideology snapshot, and law-over-politics scoring that POSTs to Horizons once the two new ingest routes exist.

**Architecture:** Pure functions classify opinions and compute scores. Workers fetch official sources, map to `judicial_events` / `judicial_scores`, and POST through `HorizonsIngestClient` the same way FJC judges already post. The browser never computes a score. Horizons UI is a paste-ready contract, not a file in this repo.

**Tech Stack:** Python 3.12, pytest, requests, GitHub Actions, Horizons ingest API (`X-Ingest-Key`).

## Global Constraints

- Spec: `docs/superpowers/specs/2026-08-15-judicial-scores-design.md`
- `source_url` required on every event. No invented matches. Ambiguous rows → `manual_review`.
- `event_type` enum: `scotus_stay`, `scotus_reversed`, `nationwide_injunction`, `vacatur_nonparty`, `standing_rebuke`, `recusal_conflict`, `circuit_reversed`, `circuit_vacated`
- `classification` enum: `explicit_text`, `higher_court_disposition`, `structured_dataset`
- Weights and bands live only in `ingest/judicial_score.py`
- LLM-only classification is not a write path
- Appointing party must never become `ideology_score`
- Workers default to `--dry-run`. Do not POST until the Horizons route returns 401 or 200, not 404
- Reuse secrets `HORIZONS_BASE_URL`, `INGEST_API_KEY` / `INGEST_KEY`
- No live CourtListener in CI
- Do not mix this work with pushing the older 10 ingest commits; those are a separate operator step
- Horizons page/routes are not implemented in this repo

## File map

| File | Responsibility |
|---|---|
| `ingest/judicial_events.py` | `event_id`, opinion text classification, review vs write |
| `ingest/judicial_score.py` | weights, recency, `score_judge`, ideology label, scorer `run()` |
| `ingest/jcs_scores.py` | refresh `ingest/data/jcs.json` from published files |
| `ingest/data/jcs.json` | checked-in ideology snapshot the scorer reads |
| `ingest/courtlistener_events.py` | fetch CL opinions, map to events or review |
| `ingest/scotus_stays.py` | map SCOTUS stay/reversal clusters to events |
| `ingest/ingest_client.py` | `submit_judicial_events`, `submit_judicial_scores` |
| `tests/test_judicial_events.py` | classification + idempotent ids |
| `tests/test_judicial_score.py` | formula golden files |
| `tests/test_jcs_scores.py` | rescale + prior_only |
| `tests/test_courtlistener_events.py` | mapping fixtures |
| `tests/test_scotus_stays.py` | stay/reversal fixtures |
| `.github/workflows/courtlistener-events.yml` | dry-run default |
| `.github/workflows/scotus-stays.yml` | dry-run default |
| `.github/workflows/jcs-scores.yml` | dry-run default |
| `.github/workflows/judicial-score.yml` | dry-run default |
| `docs/HORIZONS_JUDICIAL_SCORES.md` | paste-ready Horizons contract |

---

### Task 1: Event identity and opinion classification

**Files:**
- Create: `ingest/judicial_events.py`
- Test: `tests/test_judicial_events.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `EVENT_TYPES: tuple[str, ...]`
  - `event_id(source_url: str, event_type: str) -> str`
  - `classify_opinion_text(text: str) -> dict` with keys `status` (`write` \| `review`), `event_type` (str \| None), `classification` (str \| None), `disposition` (str \| None), `reason` (str)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_judicial_events.py
from ingest.judicial_events import classify_opinion_text, event_id


def test_event_id_is_stable_and_unique_per_type():
    url = "https://www.courtlistener.com/opinion/1/foo/"
    a = event_id(url, "nationwide_injunction")
    b = event_id(url, "nationwide_injunction")
    c = event_id(url, "vacatur_nonparty")
    assert a == b
    assert a != c
    assert len(a) == 32


def test_nationwide_injunction_explicit_text_writes():
    text = (
        "The court GRANTS a nationwide injunction restraining defendants "
        "from enforcing the rule against anyone, including non-parties."
    )
    out = classify_opinion_text(text)
    assert out["status"] == "write"
    assert out["event_type"] == "nationwide_injunction"
    assert out["classification"] == "explicit_text"
    assert out["disposition"] == "granted"


def test_bare_injunction_goes_to_review():
    text = "Plaintiffs' motion for a preliminary injunction is granted as to the named plaintiffs only."
    out = classify_opinion_text(text)
    assert out["status"] == "review"
    assert out["event_type"] is None
    assert "non-parties" in out["reason"] or "injunction" in out["reason"]


def test_vacatur_nonparty_explicit_writes():
    text = (
        "Pursuant to the APA the court VACATES the rule and sets it aside "
        "nationwide, not limited to the plaintiffs."
    )
    out = classify_opinion_text(text)
    assert out["status"] == "write"
    assert out["event_type"] == "vacatur_nonparty"


def test_empty_text_is_review():
    out = classify_opinion_text("")
    assert out["status"] == "review"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_judicial_events.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'ingest.judicial_events'`

- [ ] **Step 3: Write minimal implementation**

```python
# ingest/judicial_events.py
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_judicial_events.py -v`

Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add ingest/judicial_events.py tests/test_judicial_events.py
git commit -m "feat(judicial): classify opinion text into scored events"
```

---

### Task 2: Score formula

**Files:**
- Create: `ingest/judicial_score.py`
- Test: `tests/test_judicial_score.py`

**Interfaces:**
- Consumes: `EVENT_TYPES` from `ingest.judicial_events`
- Produces:
  - `WEIGHTS: dict[str, int]`
  - `recency_multiplier(event_date: date, today: date) -> float`
  - `score_judge(events: list[dict], *, today: date, ideology_score: int | None = None, ideology_prior_only: bool = False) -> dict`
  - Return dict keys: `rogue_score` (int), `rogue_band` (str), `event_count` (int), `raw` (float), `ideology_score` (int \| None), `ideology_label` (str \| None)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_judicial_score.py
from datetime import date

from ingest.judicial_score import recency_multiplier, score_judge

TODAY = date(2026, 8, 15)


def _event(event_type, days_ago, url="https://example.com/a"):
    from datetime import timedelta

    return {
        "event_type": event_type,
        "event_date": (TODAY - timedelta(days=days_ago)).isoformat(),
        "source_url": url,
        "weight": None,
    }


def test_three_event_golden_raw_and_score():
    events = [
        _event("nationwide_injunction", 30, "https://ex/1"),
        _event("nationwide_injunction", 40, "https://ex/2"),
        _event("scotus_stay", 10, "https://ex/3"),
    ]
    out = score_judge(events, today=TODAY)
    # raw = 4+4+5 = 13; score = round(13/20*100) = 65
    assert out["raw"] == 13.0
    assert out["rogue_score"] == 65
    assert out["rogue_band"] == "rogue"
    assert out["event_count"] == 3


def test_zero_events_is_unscored():
    out = score_judge([], today=TODAY)
    assert out["rogue_score"] == 0
    assert out["rogue_band"] == "unscored"
    assert out["event_count"] == 0


def test_sixteen_year_old_stay_contributes_zero():
    events = [_event("scotus_stay", 16 * 365 + 10)]
    out = score_judge(events, today=TODAY)
    assert out["raw"] == 0.0
    assert out["rogue_band"] == "unscored"
    assert out["event_count"] == 0


def test_recency_buckets():
    assert recency_multiplier(date(2024, 8, 15), TODAY) == 1.0
    assert recency_multiplier(date(2019, 8, 15), TODAY) == 0.6
    assert recency_multiplier(date(2014, 8, 15), TODAY) == 0.3
    assert recency_multiplier(date(2010, 8, 15), TODAY) == 0.0


def test_ideology_not_inferred_from_party():
    out = score_judge([], today=TODAY, ideology_score=None, ideology_prior_only=True)
    assert out["ideology_score"] is None
    assert out["ideology_label"] == "prior_only"


def test_ideology_labels():
    lib = score_judge([], today=TODAY, ideology_score=-40)
    mid = score_judge([], today=TODAY, ideology_score=0)
    con = score_judge([], today=TODAY, ideology_score=40)
    assert lib["ideology_label"] == "liberal"
    assert mid["ideology_label"] == "moderate"
    assert con["ideology_label"] == "conservative"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_judicial_score.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'ingest.judicial_score'`

- [ ] **Step 3: Write minimal implementation**

```python
# ingest/judicial_score.py
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_judicial_score.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ingest/judicial_score.py tests/test_judicial_score.py
git commit -m "feat(judicial): add law-over-politics score formula"
```

---

### Task 3: Horizons client writes

**Files:**
- Modify: `ingest/ingest_client.py` (add two methods after `submit_judicial_judges`)
- Test: `tests/test_judicial_client.py`

**Interfaces:**
- Consumes: event/score dicts from Tasks 1–2
- Produces:
  - `HorizonsIngestClient.submit_judicial_events(records: list[dict]) -> dict`
  - `HorizonsIngestClient.submit_judicial_scores(records: list[dict]) -> dict`
  - POST `/hcgi/api/ingest/judicial/events` body `{"events": [...]}`
  - POST `/hcgi/api/ingest/judicial/scores` body `{"scores": [...]}`

- [ ] **Step 1: Write the failing tests**

Use a tiny fake client by instantiating `HorizonsIngestClient` and monkeypatching `_post_ingest`.

```python
# tests/test_judicial_client.py
import pytest

from ingest.ingest_client import HorizonsIngestClient


class FakeResp:
    def __init__(self, status=200, data=None):
        self.status_code = status
        self._data = data or {"ok": True, "written": 1}
        self.text = ""

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


def test_submit_events_requires_source_url_and_judge_id():
    hz = HorizonsIngestClient("https://example.com", "k")
    with pytest.raises(ValueError):
        hz.submit_judicial_events([{"event_id": "x", "event_type": "scotus_stay"}])


def test_submit_events_posts_batch(monkeypatch):
    hz = HorizonsIngestClient("https://example.com", "k")
    seen = {}

    def fake_post(body, path="/hcgi/api/ingest/disclosures"):
        seen["body"] = body
        seen["path"] = path
        return FakeResp(200, {"ok": True, "written": 1})

    monkeypatch.setattr(hz, "_post_ingest", fake_post)
    rec = {
        "event_id": "abc",
        "judge_id": "13761857",
        "event_type": "nationwide_injunction",
        "event_date": "2026-01-01",
        "court": "S.D.N.Y.",
        "source_url": "https://www.courtlistener.com/opinion/1/",
        "classification": "explicit_text",
        "weight": 4,
    }
    out = hz.submit_judicial_events([rec])
    assert seen["path"] == "/hcgi/api/ingest/judicial/events"
    assert seen["body"]["events"][0]["judge_id"] == "13761857"
    assert out["written"] == 1


def test_submit_scores_posts_batch(monkeypatch):
    hz = HorizonsIngestClient("https://example.com", "k")
    seen = {}

    def fake_post(body, path="/hcgi/api/ingest/disclosures"):
        seen["path"] = path
        seen["body"] = body
        return FakeResp(200, {"ok": True, "written": 1})

    monkeypatch.setattr(hz, "_post_ingest", fake_post)
    rec = {
        "judge_id": "13761857",
        "rogue_score": 65,
        "rogue_band": "rogue",
        "event_count": 3,
        "scored_through": "2026-08-15",
    }
    hz.submit_judicial_scores([rec])
    assert seen["path"] == "/hcgi/api/ingest/judicial/scores"
    assert seen["body"]["scores"][0]["rogue_band"] == "rogue"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_judicial_client.py -v`

Expected: FAIL with `AttributeError: submit_judicial_events`

- [ ] **Step 3: Write the two methods**

Add after `submit_judicial_judges` in `ingest/ingest_client.py`. Copy the existing 404-log + raise_for_status pattern from `submit_judicial_judges`. Validate:

Events: require `source_url`, `judge_id`, `event_id`, `event_type` in the Task 1 enum, `classification` in the Task 1 enum, `event_date`, `court`, `weight` in 1–5.

Scores: require `judge_id`, `rogue_score`, `rogue_band` in (`unscored`, `within_bounds`, `watch`, `rogue`), `event_count`, `scored_through`.

Empty list returns `{"ok": True, "written": 0}` without HTTP.

On 404, log `See docs/HORIZONS_JUDICIAL_SCORES.md`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_judicial_client.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ingest/ingest_client.py tests/test_judicial_client.py
git commit -m "feat(judicial): post events and scores to Horizons ingest routes"
```

---

### Task 4: JCS / Martin-Quinn snapshot

**Files:**
- Create: `ingest/jcs_scores.py`
- Create: `ingest/data/jcs.json`
- Test: `tests/test_jcs_scores.py`

**Interfaces:**
- Consumes: nothing from Horizons
- Produces:
  - `rescale_jcs(value: float) -> int`  (clamp −1..+1 → −100..+100)
  - `lookup_ideology(snapshot: dict, judge_id: str) -> dict` with `ideology_score` (int \| None), `ideology_prior_only` (bool)
  - `load_snapshot(path: Path | None = None) -> dict`
  - Default path `ingest/data/jcs.json`
  - Snapshot shape: `{"updated": "YYYY-MM-DD", "scores": {"<judge_id>": {"jcs": 0.41, "source": "jcs"}}}`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_jcs_scores.py
from ingest.jcs_scores import lookup_ideology, rescale_jcs


def test_rescale_jcs():
    assert rescale_jcs(0.41) == 41
    assert rescale_jcs(-1) == -100
    assert rescale_jcs(1.2) == 100


def test_lookup_missing_is_not_party_score():
    out = lookup_ideology({"scores": {}}, "13761857")
    assert out["ideology_score"] is None
    assert out["ideology_prior_only"] is False


def test_lookup_known_id():
    snap = {"scores": {"1377101": {"jcs": 0.7, "source": "martin_quinn"}}}
    out = lookup_ideology(snap, "1377101")
    assert out["ideology_score"] == 70
    assert out["ideology_prior_only"] is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_jcs_scores.py -v`

Expected: FAIL `ModuleNotFoundError`

- [ ] **Step 3: Implement loader + seed file**

`ingest/data/jcs.json`:

```json
{
  "updated": "2026-08-15",
  "scores": {}
}
```

`ingest/jcs_scores.py` implements `rescale_jcs`, `lookup_ideology`, `load_snapshot`, and a `main()` that `--dry-run` prints the current snapshot count. A later live refresh can overwrite `ingest/data/jcs.json`; v1 may keep an empty scores object if the published file is not bundled. Do **not** fill scores from appointing president.

CLI: `python -m ingest.jcs_scores --dry-run`

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_jcs_scores.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ingest/jcs_scores.py ingest/data/jcs.json tests/test_jcs_scores.py
git commit -m "feat(judicial): add JCS ideology snapshot loader"
```

---

### Task 5: CourtListener event mapper

**Files:**
- Create: `ingest/courtlistener_events.py`
- Test: `tests/test_courtlistener_events.py`

**Interfaces:**
- Consumes: `classify_opinion_text`, `event_id` from Task 1; `WEIGHTS` from Task 2
- Produces:
  - `map_cluster(cluster: dict, roster: list[dict]) -> dict`  
    `status` is `write` | `review`. Write payload has full event fields including `judge_id`.
  - `match_judge(name: str, court: str, roster: list[dict]) -> str | None`  
    Exact casefold full_name + court substring. 0 or >1 matches → None.

`cluster` fixture keys: `absolute_url`, `date_filed`, `case_name`, `court`, `judges` (string), `plain_text`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_courtlistener_events.py
from ingest.courtlistener_events import map_cluster, match_judge

ROSTER = [
    {
        "judge_id": "13761857",
        "full_name": "Adam Ben Abelson",
        "court": "U.S. District Court for the District of Maryland",
    },
    {
        "judge_id": "1393931",
        "full_name": "Ronnie Abrams",
        "court": "U.S. District Court for the Southern District of New York",
    },
]


def test_match_unique_name():
    assert match_judge("Adam Ben Abelson", "District of Maryland", ROSTER) == "13761857"


def test_match_ambiguous_or_unknown_is_none():
    assert match_judge("John Smith", "District of Maryland", ROSTER) is None


def test_map_nationwide_writes_event():
    cluster = {
        "absolute_url": "/opinion/99/md-case/",
        "date_filed": "2026-02-01",
        "case_name": "Smith v. United States",
        "court": "District of Maryland",
        "judges": "Adam Ben Abelson",
        "plain_text": "The court grants a nationwide injunction against the agency.",
    }
    out = map_cluster(cluster, ROSTER)
    assert out["status"] == "write"
    assert out["event"]["judge_id"] == "13761857"
    assert out["event"]["event_type"] == "nationwide_injunction"
    assert out["event"]["source_url"].startswith("https://www.courtlistener.com/")
    assert out["event"]["weight"] == 4


def test_map_bare_injunction_is_review():
    cluster = {
        "absolute_url": "/opinion/100/md-case/",
        "date_filed": "2026-02-01",
        "case_name": "Doe v. United States",
        "court": "District of Maryland",
        "judges": "Adam Ben Abelson",
        "plain_text": "A preliminary injunction is granted as to the named plaintiffs only.",
    }
    out = map_cluster(cluster, ROSTER)
    assert out["status"] == "review"
    assert "event" not in out or out.get("event") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_courtlistener_events.py -v`

Expected: FAIL `ModuleNotFoundError`

- [ ] **Step 3: Implement mapper**

Prefix `https://www.courtlistener.com` onto `absolute_url` if needed. Call `classify_opinion_text` on `plain_text`. If write and `match_judge` returns an id, emit the event with `event_id(...)`, `weight=WEIGHTS[event_type]`, `court` from cluster. If classify is write but judge unmatched → review (`reason=unmatched_judge`). If classify is review → review.

`run()` in this task may be a stub that accepts a list of clusters (tests do not call the network). Live fetch belongs in Task 7.

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_courtlistener_events.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ingest/courtlistener_events.py tests/test_courtlistener_events.py
git commit -m "feat(judicial): map CourtListener opinions to events"
```

---

### Task 6: SCOTUS stay and reversal mapper

**Files:**
- Create: `ingest/scotus_stays.py`
- Test: `tests/test_scotus_stays.py`

**Interfaces:**
- Consumes: `event_id`, `WEIGHTS`, `match_judge` from Tasks 1, 2, 5
- Produces:
  - `map_scotus_order(order: dict, roster: list[dict]) -> dict`
  - `order` keys: `source_url`, `date_filed`, `case_name`, `lower_court`, `lower_judge`, `disposition` (`stayed` \| `reversed` \| `vacated` \| `other`)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_scotus_stays.py
from ingest.scotus_stays import map_scotus_order

ROSTER = [
    {
        "judge_id": "1393931",
        "full_name": "Ronnie Abrams",
        "court": "U.S. District Court for the Southern District of New York",
    }
]


def test_stay_writes_scotus_stay():
    out = map_scotus_order(
        {
            "source_url": "https://www.supremecourt.gov/orders/2026/stay1.pdf",
            "date_filed": "2026-03-01",
            "case_name": "United States v. Example",
            "lower_court": "S.D.N.Y.",
            "lower_judge": "Ronnie Abrams",
            "disposition": "stayed",
        },
        ROSTER,
    )
    assert out["status"] == "write"
    assert out["event"]["event_type"] == "scotus_stay"
    assert out["event"]["classification"] == "higher_court_disposition"
    assert out["event"]["weight"] == 5
    assert out["event"]["judge_id"] == "1393931"


def test_unmatched_lower_judge_is_review():
    out = map_scotus_order(
        {
            "source_url": "https://www.supremecourt.gov/orders/2026/stay2.pdf",
            "date_filed": "2026-03-01",
            "case_name": "United States v. Example",
            "lower_court": "S.D.N.Y.",
            "lower_judge": "Nobody Known",
            "disposition": "reversed",
        },
        ROSTER,
    )
    assert out["status"] == "review"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_scotus_stays.py -v`

Expected: FAIL `ModuleNotFoundError`

- [ ] **Step 3: Implement mapper**

Map `stayed` → `scotus_stay`, `reversed` → `scotus_reversed`, `vacated` → `scotus_reversed` (SCOTUS vacatur of the holding uses the same weight). `other` → review. Unmatched judge → review. `classification` is always `higher_court_disposition` on writes.

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_scotus_stays.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ingest/scotus_stays.py tests/test_scotus_stays.py
git commit -m "feat(judicial): map SCOTUS stays and reversals"
```

---

### Task 7: Worker CLIs, scorer run, and dry-run workflows

**Files:**
- Modify: `ingest/courtlistener_events.py` (add `run` + `main`)
- Modify: `ingest/scotus_stays.py` (add `run` + `main`)
- Modify: `ingest/judicial_score.py` (add `run` + `main`)
- Modify: `ingest/jcs_scores.py` (add `main` if missing)
- Create: `.github/workflows/courtlistener-events.yml`
- Create: `.github/workflows/scotus-stays.yml`
- Create: `.github/workflows/jcs-scores.yml`
- Create: `.github/workflows/judicial-score.yml`
- Test: `tests/test_judicial_score_run.py`

**Interfaces:**
- Consumes: mappers + `HorizonsIngestClient`
- Produces:
  - `python -m ingest.courtlistener_events [--dry-run] [--days-back N] [--max-records N]`
  - `python -m ingest.scotus_stays [--dry-run] [--days-back N] [--max-records N]`
  - `python -m ingest.judicial_score [--dry-run] [--max-records N]`
  - Each `run(...)` returns `{found, mapped, review, posted, dry_run}`
  - Scorer reads events from argument `events_by_judge: dict[str, list]` in tests; live path GETs `/hcgi/api/judicial/events?page=` when not dry-run, and uses fixtures/local JSON when `--dry-run`

- [ ] **Step 1: Write a failing scorer-run test**

```python
# tests/test_judicial_score_run.py
from datetime import date

from ingest.judicial_score import run


def test_run_dry_run_scores_one_judge():
    events = {
        "13761857": [
            {
                "event_type": "nationwide_injunction",
                "event_date": "2026-01-01",
                "source_url": "https://ex/1",
                "weight": 4,
            }
        ]
    }
    out = run(
        dry_run=True,
        today=date(2026, 8, 15),
        events_by_judge=events,
        snapshot={"scores": {}},
    )
    assert out["mapped"] == 1
    assert out["posted"] == 0
    assert out["dry_run"] is True
    assert out["scores"][0]["judge_id"] == "13761857"
    assert out["scores"][0]["rogue_band"] == "within_bounds"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_judicial_score_run.py -v`

Expected: FAIL (`run` missing or wrong signature)

- [ ] **Step 3: Implement `run` / `main` on the four modules**

Shared CLI pattern from `ingest/fjc.py`: argparse `--dry-run`, `--max-records`; `config.require_config(dry_run=..., source="fjc")` is acceptable for the HTTP-key check when not dry-run.

`judicial_score.run`:

- For each `judge_id`, call `score_judge`
- Attach `scored_through=today.isoformat()`
- If `dry_run`, do not POST
- Else `get_client().submit_judicial_scores(batch)` in chunks of 25
- Unknown judge ids (not in optional `known_ids` set) increment `review` and skip POST

Workflows: copy `.github/workflows/fjc-judges.yml`. Change the run line to the matching module. Keep `dry_run` default `"true"`. No cron until Horizons routes exist (comment the cron like `usaspending.yml`).

CourtListener/SCOTUS live fetch may no-op with `found=0` when `COURTLISTENER_TOKEN` is unset **in tests**. For dispatch runs, log and exit 0 on dry-run if the network is skipped. Do not call the network from pytest.

- [ ] **Step 4: Run the focused tests plus the suite slice**

Run: `pytest tests/test_judicial_score_run.py tests/test_judicial_score.py tests/test_judicial_events.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ingest/courtlistener_events.py ingest/scotus_stays.py ingest/judicial_score.py ingest/jcs_scores.py .github/workflows/courtlistener-events.yml .github/workflows/scotus-stays.yml .github/workflows/jcs-scores.yml .github/workflows/judicial-score.yml tests/test_judicial_score_run.py
git commit -m "feat(judicial): add dry-run workers and score CLI"
```

---

### Task 8: Horizons contract + README pointer

**Files:**
- Create: `docs/HORIZONS_JUDICIAL_SCORES.md`
- Modify: `README.md` (add a Judicial scores subsection after existing judicial/FJC notes, or at the end if none)

**Interfaces:**
- Consumes: spec API section
- Produces: paste-ready prompt Horizons can implement; no Python behavior change

- [ ] **Step 1: Write `docs/HORIZONS_JUDICIAL_SCORES.md`**

Include:

- Live probe note: workers stay dry-run while POST returns 404
- Collections `judicial_events` and `judicial_scores` (fields from the spec)
- `POST /hcgi/api/ingest/judicial/events` and `.../scores` with `X-Ingest-Key`
- Public GETs: judges join, `GET /judicial/judges/:judge_id`, `GET /judicial/events`, `GET /judicial/watch`
- Query params and default sort `rogue`
- UI bullets: watch strip, badge copy (`insufficient record` / `within bounds` / `watch` / `rogue`), lean meter, `/judicial/:judge_id`, methodology anchor
- Thin-data rules from the spec
- Paste-ready prompt block in the same style as `docs/HORIZONS_JUDICIAL_JUDGES.md`

Do not invent extra fields.

- [ ] **Step 2: Add a short README section**

Title: `## Judicial scores`

State: roster is FJC; events and scores are scored in this repo; Horizons must implement `docs/HORIZONS_JUDICIAL_SCORES.md` before `--post`. Commands:

```
python -m ingest.courtlistener_events --dry-run --max-records 5
python -m ingest.scotus_stays --dry-run --max-records 5
python -m ingest.judicial_score --dry-run
```

- [ ] **Step 3: Commit**

```bash
git add docs/HORIZONS_JUDICIAL_SCORES.md README.md
git commit -m "docs: Horizons contract for judicial events and scores"
```

---

### Task 9: Full offline test gate

**Files:**
- None new unless a test failed to import

- [ ] **Step 1: Run the judicial test slice**

Run: `pytest tests/test_judicial_events.py tests/test_judicial_score.py tests/test_jcs_scores.py tests/test_courtlistener_events.py tests/test_scotus_stays.py tests/test_judicial_client.py tests/test_judicial_score_run.py -v`

Expected: PASS, no network

- [ ] **Step 2: Fix any import or fixture failures from earlier tasks. Re-run until green.**

- [ ] **Step 3: Commit only if fixes were required**

```bash
git add -u
git commit -m "test(judicial): fix score ingest slice"
```

Skip the commit if the tree is clean.

---

## Horizons-only follow-up (not this plan)

After Task 8, paste `docs/HORIZONS_JUDICIAL_SCORES.md` into Horizons. Probe:

```
POST https://rollcalltrack.com/hcgi/api/ingest/judicial/events
```

Ready when status is 401 (bad key) or 200, not 404. Then flip workflow `dry_run` to `false` by dispatch. UI work (watch strip, badges, detail page, methodology) stays in Horizons.

JEFS `recusal_conflict` events wait until judicial filings ingest is posting.

## Self-review

- Spec coverage: event rules, weights, bands, ideology prior, collections, APIs, workers, tests, dry-run gate, Horizons split, error/review path — each has a task. Page UI is Task 8 copy + follow-up, not Python.
- No TBD/TODO placeholders.
- Names are consistent: `event_id`, `score_judge`, `submit_judicial_events`, `submit_judicial_scores`, `map_cluster`, `map_scotus_order`.
