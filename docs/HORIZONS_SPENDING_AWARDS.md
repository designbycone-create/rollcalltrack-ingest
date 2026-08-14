# Horizons: `/hcgi/api/ingest/spending/awards`

The ingest job `python -m ingest.usaspending` POSTs batches to this route
with header `X-Ingest-Key`. As of 2026-08-13 the live site returns
`404 {"error":"Route not found"}`. Implement this on Horizons before
turning on the daily workflow for real writes.

## Request

`POST /hcgi/api/ingest/spending/awards`

```json
{
  "awards": [
    {
      "award_id": "CONT_AWD_EPW17009_6800_-NONE-_-NONE-",
      "recipient_name": "ABT GLOBAL LLC",
      "recipient_state": "MA",
      "awarding_agency": "Environmental Protection Agency",
      "award_type": "contract",
      "amount": 7655172.98,
      "amount_type": "obligated",
      "period_of_performance_start": "2017-02-11",
      "period_of_performance_end": "2023-03-10",
      "description": "NCEE mission support",
      "source_url": "https://www.usaspending.gov/award/CONT_AWD_EPW17009_6800_-NONE-_-NONE-",
      "fiscal_year": 2026
    }
  ]
}
```

## Rules to enforce (same philosophy as disclosures)

- `source_url` required, unique upsert key (re-POST updates, no duplicates)
- `award_id` required (USAspending `generated_internal_id`)
- `award_type` enum: `contract`, `grant`, `loan`, `direct_payment`, `insurance`
- `amount_type` enum: `obligated`, `potential`
- `amount` is the official obligated figure from USAspending `Award Amount`.
  Do not compute a midpoint. Loans are not sent here (see ingest notes).
- dates ISO `YYYY-MM-DD`; `period_of_performance_end` nullable
- `recipient_state` is a 2-letter USPS code or null
- reject unknown enum values with 400 + field errors

## Suggested response

```json
{ "ok": true, "written": 1, "updated": 0 }
```

## Paste-ready Horizons prompt

```
Add POST /hcgi/api/ingest/spending/awards authenticated by X-Ingest-Key
(same key as /hcgi/api/ingest/disclosures).

Accept a JSON body { "awards": [ ... ] } where each award has:
award_id (string, required), recipient_name, recipient_state (2-letter or null),
awarding_agency, award_type enum [contract, grant, loan, direct_payment, insurance],
amount (number), amount_type enum [obligated, potential],
period_of_performance_start (YYYY-MM-DD), period_of_performance_end (YYYY-MM-DD or null),
description, source_url (required, unique), fiscal_year (int).

Upsert on source_url. Re-POST of the same source_url updates the existing row
and keeps the same record id. Reject missing source_url or bad enums with 400
and field-level errors. Do not invent or average dollar amounts.

Return { ok, written, updated }.
```
