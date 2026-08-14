# Horizons: `/hcgi/api/ingest/judicial/judges`

Live `POST https://rollcalltrack.com/hcgi/api/ingest/judicial/judges`
returns `404 {"error":"Route not found"}` as of 2026-08-14.

This route must be requested from the Horizons side before unattended
writes. Upsert key is `judge_id` (FJC `nid`). `source_url` is required.

## Request

`POST /hcgi/api/ingest/judicial/judges`  
Header: `X-Ingest-Key`

```json
{
  "judges": [
    {
      "judge_id": "13761857",
      "full_name": "Adam Ben Abelson",
      "court": "U.S. District Court for the District of Maryland",
      "court_type": "district_court",
      "appointing_president": "Joseph R. Biden",
      "nomination_date": "2024-05-14",
      "confirmation_date": "2024-09-10",
      "aba_rating": "Well Qualified",
      "status": "active",
      "source_url": "https://www.fjc.gov/node/13761857"
    }
  ]
}
```

## Rules

- `judge_id` required; unique upsert key. This is FJC's `nid` (Node ID),
  not the legacy `jid`. Later judicial jobs join on this value.
- `source_url` required; the official FJC bio page
  `https://www.fjc.gov/node/{nid}`
- `court_type` enum: `supreme_court`, `circuit_court`, `district_court`,
  `bankruptcy_court`, `magistrate`, `other`
- `status` enum: `active`, `senior_status`, `retired`, `resigned`,
  `deceased`
- `nomination_date` / `confirmation_date` ISO `YYYY-MM-DD` or null
- `aba_rating` / `appointing_president` nullable
- Reject missing `judge_id` / `source_url` or bad enums with 400

## Paste-ready Horizons prompt

```
Add POST /hcgi/api/ingest/judicial/judges authenticated by X-Ingest-Key
(same key as /hcgi/api/ingest/disclosures).

Accept { "judges": [ ... ] } with:
judge_id (required, unique upsert — FJC nid),
full_name,
court,
court_type enum [supreme_court, circuit_court, district_court,
bankruptcy_court, magistrate, other],
appointing_president (string or null),
nomination_date (YYYY-MM-DD or null),
confirmation_date (YYYY-MM-DD or null),
aba_rating (string or null),
status enum [active, senior_status, retired, resigned, deceased],
source_url (required — FJC bio page https://www.fjc.gov/node/{nid}).

Reject missing judge_id/source_url or bad enums with 400 + field errors.
Do not invent dates or ABA ratings.

Return { ok, written, updated }.
```
