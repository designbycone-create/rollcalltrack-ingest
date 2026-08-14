# Horizons: `/hcgi/api/ingest/spending/fraud-cases`

Live `POST https://rollcalltrack.com/hcgi/api/ingest/spending/fraud-cases`
returns `404 {"error":"Route not found"}` as of 2026-08-14.

This table names defendants. Reject anything without a trusted `source_url`.

## Request

`POST /hcgi/api/ingest/spending/fraud-cases`  
Header: `X-Ingest-Key`

```json
{
  "fraud_cases": [
    {
      "case_title": "Dallas-Based Bus Companies Settle PPP Lawsuit for $3.9 Million",
      "agency_program": "Paycheck Protection Program (PPP)",
      "defendant_type": "company",
      "amount_alleged": 3900000.0,
      "status": "settled",
      "source_url": "https://www.justice.gov/usao-ndtx/pr/dallas-based-bus-companies-settle-ppp-lawsuit-39-million",
      "filing_date": "2026-08-12"
    }
  ]
}
```

## Rules

- `source_url` required; upsert key. Hostname must be one of:
  `pandemicoversight.gov`, `pandemic.oversight.gov`, `oversight.gov`,
  `justice.gov`, `courtlistener.com`, `uscourts.gov` (and www/subdomains).
- `status` enum: `charged`, `convicted`, `settled`, `ongoing`
- `defendant_type` enum: `individual`, `company`, `multiple`
- `amount_alleged` nullable; never invent or take “over/more than” figures
- `filing_date` ISO `YYYY-MM-DD`

## Paste-ready Horizons prompt

```
Add POST /hcgi/api/ingest/spending/fraud-cases authenticated by X-Ingest-Key
(same key as /hcgi/api/ingest/disclosures).

Accept { "fraud_cases": [ ... ] } with:
case_title, agency_program, defendant_type enum [individual, company, multiple],
amount_alleged (number or null), status enum [charged, convicted, settled, ongoing],
source_url (required, unique upsert), filing_date (YYYY-MM-DD).

Reject missing source_url or bad enums with 400 + field errors.
Do not invent dollar amounts. This table names defendants — keep
unpublished/review records out of the public collection.

Return { ok, written, updated }.
```
