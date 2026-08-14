# Horizons: `/hcgi/api/ingest/spending/ig-reports`

Live `POST https://rollcalltrack.com/hcgi/api/ingest/spending/ig-reports`
returns `404 {"error":"Route not found"}` as of 2026-08-14.

## Request

`POST /hcgi/api/ingest/spending/ig-reports`  
Header: `X-Ingest-Key`

```json
{
  "ig_reports": [
    {
      "agency": "General Services Administration",
      "report_title": "The Processes FAS Uses to Ensure It Awards and Collects Accurate Product Data Are Ineffective",
      "report_type": "audit",
      "financial_impact": 0.0,
      "summary": "FAS catalog data contains inconsistent manufacturer part numbers. Identical products cannot be compared reliably across systems.",
      "recommendations_count": 6,
      "source_url": "https://www.oversight.gov/reports/audit/processes-fas-uses-ensure-it-awards-and-collects-accurate-product-data-are",
      "publish_date": "2026-08-13"
    }
  ]
}
```

## Rules

- `source_url` required; upsert on `source_url`
- `report_type` enum: `audit`, `investigation`, `evaluation`, `semiannual_report`
- `financial_impact` nullable. Only a figure the report published. Do not
  sum questioned costs + funds for better use when they differ.
- `recommendations_count` nullable
- `publish_date` ISO `YYYY-MM-DD`

## Paste-ready Horizons prompt

```
Add POST /hcgi/api/ingest/spending/ig-reports authenticated by X-Ingest-Key
(same key as /hcgi/api/ingest/disclosures).

Accept { "ig_reports": [ ... ] } with:
agency, report_title, report_type enum [audit, investigation, evaluation,
semiannual_report], financial_impact (number or null), summary, 
recommendations_count (int or null), source_url (required, unique upsert),
publish_date (YYYY-MM-DD).

Reject missing source_url or bad enums with 400 + field errors.
Do not invent dollar amounts.

Return { ok, written, updated }.
```
