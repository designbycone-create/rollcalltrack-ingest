# Horizons: judicial events and scores

Workers stay `--dry-run` while these POSTs return `404`. Ready when a
probe with a bad key returns `401`, or a good key returns `200`.

Auth: header `X-Ingest-Key` (same secret as `/hcgi/api/ingest/disclosures`).
Public GET routes need no key. Collection create/update/delete stay API-only.

## Collections

### `judicial_events`

Upsert on `event_id` (sha256 of `source_url|event_type`, 32 hex chars).

| Field | Type | Required |
|---|---|---|
| `event_id` | text | yes |
| `judge_id` | text | yes (FJC nid) |
| `event_type` | select | `scotus_stay`, `scotus_reversed`, `nationwide_injunction`, `vacatur_nonparty`, `standing_rebuke`, `recusal_conflict`, `circuit_reversed`, `circuit_vacated` |
| `disposition` | select | `granted`, `denied`, `reversed`, `vacated`, `stayed`, `n/a` |
| `event_date` | text `YYYY-MM-DD` | yes |
| `court` | text | yes |
| `case_caption` | text | no |
| `source_url` | url | yes, unique |
| `classification` | select | `explicit_text`, `higher_court_disposition`, `structured_dataset` |
| `weight` | number 1–5 | yes |

### `judicial_scores`

Upsert on `judge_id`. Written only by `python -m ingest.judicial_score`.

| Field | Type | Required |
|---|---|---|
| `judge_id` | text | yes |
| `rogue_score` | number 0–100 | yes |
| `rogue_band` | select | `unscored`, `within_bounds`, `watch`, `rogue` |
| `ideology_score` | number or null | no (−100…+100) |
| `ideology_label` | select or null | `liberal`, `moderate`, `conservative`, `prior_only` |
| `event_count` | number | yes |
| `scored_through` | text `YYYY-MM-DD` | yes |

Do not add score columns to `judicial_judges`. Join on `judge_id`.

## Write APIs

`POST /hcgi/api/ingest/judicial/events`

```json
{ "events": [ { "event_id": "...", "judge_id": "...", "event_type": "nationwide_injunction", "event_date": "2026-02-01", "court": "D. Md.", "source_url": "https://www.courtlistener.com/opinion/99/", "classification": "explicit_text", "weight": 4 } ] }
```

`POST /hcgi/api/ingest/judicial/scores`

```json
{ "scores": [ { "judge_id": "13761857", "rogue_score": 20, "rogue_band": "within_bounds", "event_count": 1, "scored_through": "2026-08-15" } ] }
```

Reject missing `judge_id` / `source_url` / bad enums with 400. Reject a score whose `event_count` does not match stored events for that judge.

## Read APIs

```
GET /hcgi/api/judicial/judges
GET /hcgi/api/judicial/judges/:judge_id
GET /hcgi/api/judicial/events?judge_id=&event_type=&page=
GET /hcgi/api/judicial/watch?limit=25
```

Judges list query params: `court_type`, `status`, `band`, `lean`, `q`, `sort` (`rogue` default, `ideology`, `name`, `confirmed`).

Join onto each judge when a score row exists: `rogue_score`, `rogue_band`, `ideology_score`, `ideology_label`, `event_count`, `scored_through`. Unscored judges still return. `rogue_band` is `unscored` until events exist.

`/watch` returns active/senior only, bands `watch` or `rogue`, sorted `rogue_score` desc.

Judge detail returns roster + score + last 25 events.

## Page (`/judicial`)

- Hero: judges who put political preference over the law.
- Watch strip: 8–12 cards from `/judicial/watch`. Hidden when empty. No fake top list.
- Tabs stay Judges / Financial Filings / Nominations.
- Card: name, court, appointing president, rogue badge + number, lean meter, event counts, source links.
- Badge copy: `insufficient record` / `within bounds` / `watch` / `rogue`.
- `/judicial/:judge_id` timeline with caption, date, type, weight, `source_url`.
- Default sort is rogue descending. Ideology sort is opt-in.
- Footer link: `/sources-and-methodology#judicial`.
- If judges exist but no scores: “Scores publish after the first event ingest.”
- `scored_through` older than 7 days: banner, do not hide numbers.
- Review-queue rows never appear.

## Paste-ready Horizons prompt

```
Add judicial score ingest + read APIs, same X-Ingest-Key as /hcgi/api/ingest/disclosures.

Collections:
- judicial_events upsert on event_id. Fields: event_id, judge_id (FJC nid),
  event_type enum [scotus_stay, scotus_reversed, nationwide_injunction,
  vacatur_nonparty, standing_rebuke, recusal_conflict, circuit_reversed,
  circuit_vacated], disposition enum [granted, denied, reversed, vacated,
  stayed, n/a], event_date YYYY-MM-DD, court, case_caption, source_url
  (required, unique), classification enum [explicit_text,
  higher_court_disposition, structured_dataset], weight 1-5.
- judicial_scores upsert on judge_id. Fields: judge_id, rogue_score 0-100,
  rogue_band enum [unscored, within_bounds, watch, rogue], ideology_score
  nullable -100..100, ideology_label enum [liberal, moderate, conservative,
  prior_only] or null, event_count, scored_through YYYY-MM-DD.
Do not add score columns to judicial_judges.

POST /hcgi/api/ingest/judicial/events  body { "events": [ ... ] }
POST /hcgi/api/ingest/judicial/scores  body { "scores": [ ... ] }
Reject missing judge_id/source_url/event_id or bad enums with 400.
Reject a score if event_count does not match stored events for that judge.

GET /hcgi/api/judicial/judges joins score fields when present. Unscored
judges still return (rogue_band=unscored). Query: court_type, status, band,
lean, q, sort=rogue|ideology|name|confirmed (default rogue).
GET /hcgi/api/judicial/judges/:judge_id returns roster + score + last 25 events.
GET /hcgi/api/judicial/events filters judge_id, event_type, page.
GET /hcgi/api/judicial/watch is active/senior, band watch|rogue, rogue_score desc.

Update /judicial: watch strip from /watch (hide if empty), rogue badge +
lean meter on cards, /judicial/:judge_id timeline, methodology link
/sources-and-methodology#judicial. Badge copy: insufficient record /
within bounds / watch / rogue. No placeholder top-judges list.
```
