# Horizons: `/hcgi/api/ingest/spending/improper-payments`

Live `POST https://rollcalltrack.com/hcgi/api/ingest/spending/improper-payments`
returns `404 {"error":"Route not found"}` as of 2026-08-14. Implement this
before a non-dry-run dispatch.

## Request

`POST /hcgi/api/ingest/spending/improper-payments`  
Header: `X-Ingest-Key` (same key as disclosures)

```json
{
  "improper_payments": [
    {
      "agency": "Department of Health and Human Services",
      "program_name": "Centers for Medicare & Medicaid Services (CMS) - Medicaid",
      "fiscal_year": 2025,
      "estimated_amount_low": 35029540000.0,
      "estimated_amount_high": 35029540000.0,
      "cause_category": "overpayment",
      "source_report_title": "Payment Integrity: Agencies' Estimated Improper Payments Increased to $186 Billion in Fiscal Year 2025",
      "source_url": "https://www.gao.gov/products/gao-26-108694",
      "report_date": "2026-04-27"
    }
  ]
}
```

## Rules

- `source_url` required (GAO product page). Upsert key should be
  `(source_url, fiscal_year, program_name, cause_category)`.
- `cause_category` enum: `overpayment`, `underpayment`, `fraud`,
  `documentation_error`, `other`
- `estimated_amount_low` / `estimated_amount_high` are published dollars.
  When the source gives a single figure, both fields are that figure.
  Do not invent a midpoint.
- `report_date` ISO `YYYY-MM-DD`

## Paste-ready Horizons prompt

```
Add POST /hcgi/api/ingest/spending/improper-payments authenticated by
X-Ingest-Key (same key as /hcgi/api/ingest/disclosures).

Accept { "improper_payments": [ ... ] } where each record has:
agency, program_name, fiscal_year (int), estimated_amount_low (number),
estimated_amount_high (number or null), cause_category enum
[overpayment, underpayment, fraud, documentation_error, other],
source_report_title, source_url (required), report_date (YYYY-MM-DD).

Upsert on (source_url, fiscal_year, program_name, cause_category).
Reject missing source_url or bad enums with 400 + field errors.
Do not average or invent dollar amounts.

Return { ok, written, updated }.
```
