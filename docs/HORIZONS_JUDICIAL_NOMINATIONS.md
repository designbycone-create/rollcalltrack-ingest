# Horizons: `/hcgi/api/ingest/judicial/nominations`

Live `POST https://rollcalltrack.com/hcgi/api/ingest/judicial/nominations`
returns 200 as of 2026-08-18. Public `GET /hcgi/api/judicial/nominations`
exists (was empty until the first POST).

This job links a Congress.gov PN to an FJC `judge_id` and, when the
confirmation was a recorded roll call, to an existing Senate vote id
(`s-{congress}-{year}-{roll}`, e.g. `s-119-2026-209`).

## Request

`POST /hcgi/api/ingest/judicial/nominations`  
Header: `X-Ingest-Key`

```json
{
  "nominations": [
    {
      "nomination_id": "PN999-5",
      "judge_id": "9990001",
      "nominating_president": "Donald J. Trump",
      "vote_date": "2026-07-23",
      "vote_result": "confirmed",
      "senate_vote_id": "s-119-2026-209",
      "senate_vote_note": "linked",
      "source_url": "https://www.congress.gov/nomination/119th-congress/999"
    }
  ]
}
```

## Rules

- `nomination_id` required; unique upsert (Congress.gov citation, e.g. `PN999-5`)
- `judge_id` required on written rows — FJC `nid`. Ambiguous name matches
  are not posted (manual_review).
- `source_url` required — Congress.gov nomination page
- `vote_result` enum: `confirmed`, `rejected`, `withdrawn`, `returned`
- `senate_vote_id` nullable. Null is valid. Do not invent a roll-call id
  for voice vote / unanimous consent.
- `senate_vote_note` tells reviewers *why* the id is null or how it was
  linked: `linked`, `voice_vote`, `unanimous_consent`,
  `roll_call_constructed`, `roll_call_unmatched`, `no_recorded_vote`

## Paste-ready Horizons prompt

```
Add POST /hcgi/api/ingest/judicial/nominations authenticated by X-Ingest-Key
(same key as /hcgi/api/ingest/disclosures).

Accept { "nominations": [ ... ] } with:
nomination_id (required, unique upsert — Congress.gov PN citation),
judge_id (FJC nid),
nominating_president (string or null),
vote_date (YYYY-MM-DD or null),
vote_result enum [confirmed, rejected, withdrawn, returned],
senate_vote_id (string or null — existing vote id like s-119-2026-209),
senate_vote_note (string or null — voice_vote | unanimous_consent |
linked | roll_call_constructed | roll_call_unmatched | no_recorded_vote),
source_url (required — https://www.congress.gov/nomination/…).

Reject missing nomination_id/source_url or bad enums with 400.
A null senate_vote_id is valid (voice vote / UC). Do not require it.

Return { ok, written, updated }.
```
