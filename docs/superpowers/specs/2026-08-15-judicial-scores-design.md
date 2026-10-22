# Judicial scores: law-over-politics + ideology lean

Date: 2026-08-15  
Repo: `designbycone-create/rollcalltrack-ingest`  
Site: https://rollcalltrack.com/judicial  
Status: design approved in conversation; waiting on spec review before implementation

## Decision

Two independent scores on every active Article III judge.

- **Law-over-politics (headline).** 0–100 plus band `unscored` / `within_bounds` / `watch` / `rogue`. Default sort. Answers whether the judge kept the remedy inside the case and the statute.
- **Ideology lean (secondary).** −100…+100, liberal ← 0 → conservative. Never the default sort.

They are not the same number. A conservative who issues a nationwide injunction and a liberal who does the same score the same way on the headline if the conduct matches.

Appointing party alone is not a score. Silence is not innocence: zero events is `unscored` / “insufficient record,” not “clean.”

## Who is scored

Active and senior-status Article III judges (Supreme Court, circuits, districts).

Bankruptcy, magistrate, retired, resigned, and deceased judges stay in the FJC roster. They are not scored until a later pass.

## GitHub vs Horizons

This feature is split on purpose. The ingest repo can ship almost all of the brains without opening Horizons, but it cannot create PocketBase collections or paint the page.

| Work | Owner | Can we do it from GitHub only? |
|---|---|---|
| FJC roster job (already written) | GitHub Actions in this repo | Yes |
| CourtListener / SCOTUS event scrapers | This repo + new workflows | Yes |
| JCS / Martin-Quinn ideology ingest | This repo + new workflow | Yes |
| Scorer job + golden-file tests | This repo | Yes |
| `docs/HORIZONS_JUDICIAL_SCORES.md` contract | This repo | Yes |
| `POST /hcgi/api/ingest/judicial/events` | Horizons | No. Paste the spec; wait for 401-with-bad-key, not 404 |
| `POST /hcgi/api/ingest/judicial/scores` | Horizons | No. Same |
| Join scores onto `GET /hcgi/api/judicial/judges` | Horizons | No |
| `GET /hcgi/api/judicial/watch` and judge detail | Horizons | No |
| `/judicial` watch strip, badges, lean meter, `/judicial/:judge_id` | Horizons UI | No |
| `/sources-and-methodology#judicial` | Horizons UI | No |

**GitHub-side configuration this repo will own**

- New modules under `ingest/`: `courtlistener_events.py`, `scotus_stays.py`, `jcs_scores.py`, `judicial_score.py`
- New workflows, same shape as `fjc-judges.yml`: `workflow_dispatch` first, `--dry-run` default **true** until the Horizons route returns non-404, then a daily cron
- Secrets already present and reused: `HORIZONS_BASE_URL` and `INGEST_API_KEY` (`INGEST_KEY` is still read for one release when `INGEST_API_KEY` is unset). Add `COURTLISTENER_TOKEN` only if the public API rate-limits us
- Tests under `tests/test_judicial_events.py` and `tests/test_judicial_score.py`. No live CourtListener in CI
- Dry-run writes a JSON summary to the Actions log. No POST

Workers stay `--dry-run` until a probe sees the ingest route return `401` (bad key) or `200` (good key), not `404`. That is the same gate we used for spending.

Horizons work is a paste of `docs/HORIZONS_JUDICIAL_SCORES.md` (written in the same implementation pass as the workers). This spec is the source of truth for that file.

## Event rule

A row is written to `judicial_events` only if all five hold:

1. Public official document (opinion, order, SCOTUS stay, JEFS filing, Congress.gov page)
2. Attached to one FJC `judge_id` (`nid`). Name-only matches do not write
3. `event_type` is in the enum below
4. `classification` is `explicit_text`, `higher_court_disposition`, or `structured_dataset`. LLM-only classification is not a write path
5. `source_url` is unique. Re-ingest updates. It does not double-count

Ambiguous authorship, “injunction” hits that do not bind non-parties, and unmatched names go to `manual_review`. They do not enter the score until approved.

## Allowed sources

| Use | Source |
|---|---|
| Identity | FJC `judges.csv` (live) |
| Nominations | Congress.gov + existing Senate vote ids |
| Ethics / holdings | JEFS `pub.jefs.uscourts.gov` |
| Opinions, authorship, citations | CourtListener / Free Law Project |
| Reversals and emergency stays | SCOTUS slip opinions + orders; CourtListener SCOTUS clusters |
| Ideology | Judicial Common Space; Martin-Quinn for Justices |
| Lean prior only | Appointing president + confirmation Senate (Voteview) |

**Not score inputs:** Heritage / ACLU / similar scorecards, news adjectives, tweet counts, an LLM declaring an opinion liberal, appointing party by itself. Those may appear as related reading. They never move either number.

## Event types and weights

| `event_type` | Weight | How it is proven |
|---|---|---|
| `scotus_stay` | 5 | SCOTUS order stays this judge’s order |
| `scotus_reversed` | 5 | SCOTUS reverses or summarily reverses this judge |
| `nationwide_injunction` | 4 | Order text grants an injunction binding non-parties |
| `vacatur_nonparty` | 4 | Vacatur / APA set-aside that runs beyond the parties |
| `standing_rebuke` | 3 | Reversing opinion says standing was lacking |
| `recusal_conflict` | 3 | JEFS holding overlaps a case party; no recusal |
| `circuit_reversed` | 2 | Circuit reverses this judge’s order |
| `circuit_vacated` | 2 | Circuit vacates this judge’s order |

Recency on `event_date`: 1.0 if < 5 years, 0.6 if 5–10, 0.3 if 10–15, 0 if older than 15. Old events stay on the timeline.

```
raw   = Σ (weight × recency)
score = min(100, round(raw / 20 × 100))
```

One nationwide injunction = 20. A stay plus two universals = 65.

## Bands

| Band | Rule | Badge copy |
|---|---|---|
| `unscored` | 0 events in the 15-year window | insufficient record |
| `within_bounds` | 1–34 | within bounds |
| `watch` | 35–64 | watch |
| `rogue` | 65–100 | rogue |

## Ideology

Published JCS (circuits/districts) or Martin-Quinn (Justices) rescaled to −100…+100.

- `liberal` ≤ −30
- `moderate` −29…29
- `conservative` ≥ 30

If only appointing president is known, show a **prior** labeled as prior. Do not emit `ideology_score` from party alone.

## Collections

`judicial_judges` is unchanged. Scores are not columns on the FJC row.

### `judicial_events`

Upsert key: `event_id` (stable hash of `source_url` + `event_type`).

| Field | Type | Required |
|---|---|---|
| `event_id` | text | yes |
| `judge_id` | text | yes (FJC nid) |
| `event_type` | select | yes (enum above) |
| `disposition` | select | no (`granted`, `denied`, `reversed`, `vacated`, `stayed`, `n/a`) |
| `event_date` | text `YYYY-MM-DD` | yes |
| `court` | text | yes |
| `case_caption` | text | no |
| `source_url` | url | yes, unique |
| `classification` | select | yes (`explicit_text`, `higher_court_disposition`, `structured_dataset`) |
| `weight` | number | yes, 1–5, from the table, not per-row taste |

### `judicial_scores`

Upsert key: `judge_id`. Written only by the scorer job.

| Field | Type | Required |
|---|---|---|
| `judge_id` | text | yes |
| `rogue_score` | number 0–100 | yes |
| `rogue_band` | select | yes |
| `ideology_score` | number or null | no |
| `ideology_label` | select or null | no (`liberal`, `moderate`, `conservative`, `prior_only`) |
| `event_count` | number | yes |
| `scored_through` | text `YYYY-MM-DD` | yes |

`createRule` / `updateRule` / `deleteRule` = null (API-only). List/view public except `manual_review_queue`.

## APIs

Auth on writes: `X-Ingest-Key`. Public GETs need no key.

```
POST /hcgi/api/ingest/judicial/events     { "events": [ ... ] }
POST /hcgi/api/ingest/judicial/scores     { "scores": [ ... ] }

GET  /hcgi/api/judicial/judges            roster + joined score fields when present
GET  /hcgi/api/judicial/judges/:judge_id  roster + score + last 25 events
GET  /hcgi/api/judicial/events            filter judge_id, event_type, page
GET  /hcgi/api/judicial/watch             active/senior, band watch|rogue, rogue_score desc
```

Judges list query params: `court_type`, `status`, `band`, `lean`, `q`, `sort` (`rogue` default, `ideology`, `name`, `confirmed`).

Unscored judges still return. `rogue_band` is `unscored` until the threshold.

The scorer POST is rejected if `judge_id` is missing or if `event_count` does not match stored events for that judge.

## Page

`/judicial` is the rogue-judge desk, not a quiet directory.

1. Hero states the purpose: judges who put political preference over the law.
2. Watch strip: 8–12 cards from `/judicial/watch`. Hidden while that list is empty. No placeholder “top judges.”
3. Tabs stay **Judges** (default), **Financial Filings**, **Nominations**.
4. Judge card: name, court, appointing president, rogue badge + number, lean meter, event counts, source links.
5. `/judicial/:judge_id`: event timeline (caption, date, type, weight, `source_url`), lean, methodology one-liner, filings and confirmation vote when present.
6. Filters in the toolbar. Default sort is rogue descending.
7. Footer link to `/sources-and-methodology#judicial`.

Thin-data behavior:

- Roster loaded, no scores: keep “No judicial records published yet” only if the judges list is empty. If judges exist, show them with “Scores publish after the first event ingest.”
- Roster + no events: badge `insufficient record`; lean prior only if JCS/MQ exists.
- `scored_through` older than 7 days: banner. Do not hide numbers.
- Review-queue rows never appear on the page.

Out of scope for this pass: public tip form, user-submitted scores, compare-two-judges, LLM opinion blurbs on the card.

## GitHub workers

| Job | Module | Input | Output |
|---|---|---|---|
| `fjc-judges-sync` | `ingest/fjc.py` | FJC CSV | `POST .../judicial/judges` (exists) |
| `jcs-scores` | `ingest/jcs_scores.py` | Published JCS + Martin-Quinn files | refreshes `ingest/data/jcs.json` (checked in). The scorer reads that file. Ideology is not written as a `judicial_events` row |
| `courtlistener-events` | `ingest/courtlistener_events.py` | CourtListener opinions/clusters | `POST .../judicial/events` |
| `scotus-stays` | `ingest/scotus_stays.py` | SCOTUS orders + CL SCOTUS clusters | `POST .../judicial/events` |
| `judicial-score` | `ingest/judicial_score.py` | GET events + ideology snapshot | `POST .../judicial/scores` |

CLI flags match the rest of the repo: `--dry-run`, `--days-back`, `--max-records`.

Judge matching uses FJC `nid` already stored on `judicial_judges`. CourtListener judge ids map through a local table built from name + court + appointment year. Ambiguous → `manual_review`.

## Formula module

Weights, recency, and band cuts live in `ingest/judicial_score.py` only. The methodology page quotes that module. Changing a weight is a code + docs change, not a silent dashboard tweak.

No extra points for party. No penalty for ruling against the sitting president unless an event type above fires. No averaging of partisan scorecards.

## Tests (required before the page claims a score)

- Same `source_url` ingested twice → one event
- Opinion that says “injunction” but does not bind non-parties → `manual_review`, score unchanged
- Fixture of 3 weighted events → exact `raw` and `score`
- Judge with 0 events → `unscored`, still listed
- 16-year-old stay → timeline only, contributes 0 to `raw`
- Ideology snapshot without JCS → `prior_only` or null, never a party-derived integer

CI runs these offline. Live scrapes are `workflow_dispatch` only until routes exist.

## Ship order

1. Push the existing 10 unpushed ingest commits (FJC, JEFS, nominations, spending). Separate from scores.
2. Add `docs/HORIZONS_JUDICIAL_SCORES.md` (this spec’s API section, paste-ready).
3. GitHub: `jcs_scores`, `courtlistener_events`, `scotus_stays`, `judicial_score` + tests + dry-run workflows.
4. Horizons: collections + ingest/read routes. Probe until non-404.
5. Flip workflows off dry-run. Run scorer.
6. Horizons UI: watch strip, scored cards, filters, `/judicial/:judge_id`, methodology section.
7. JEFS recusal events, once filings are flowing.

## Error handling

- Missing `source_url` / `judge_id` / bad enum → 400, no write
- Horizons 404 → worker logs the HORIZONS_*.md hint and exits 0 on dry-run, exits 1 on `--post`
- Horizons 403 mid-batch (seen on GAO) → retry with backoff, resume leftover ids, do not crash the already-written prefix without a log
- Ingestion log 500 (current live bug) → warn, do not fail the job
- Scorer finds events for an unknown `judge_id` → `manual_review`, skip that score row

## Non-goals

Scoring state judges. Scoring magistrate/bankruptcy in v1. Predicting future votes. A single left/right ranking of “best judges.”
