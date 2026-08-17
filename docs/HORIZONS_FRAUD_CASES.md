# Horizons: fraud cases — 50-state / all-agency build-out

`POST /hcgi/api/ingest/spending/fraud-cases` is live. Ingest now sends
two extra fields so the Fraud section can show **every state** and the
**federal victim agency**, not just a flat national list.

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
      "agency": "Small Business Administration",
      "state_code": "TX",
      "defendant_type": "company",
      "amount_alleged": 3900000.0,
      "status": "settled",
      "source_url": "https://www.justice.gov/usao-ndtx/pr/dallas-based-bus-companies-settle-ppp-lawsuit-39-million",
      "filing_date": "2026-08-12"
    }
  ]
}
```

## New / required fields

| Field | Type | Required | Notes |
|-------|------|----------|--------|
| existing fields | | | unchanged (`case_title`, `agency_program`, `defendant_type`, `amount_alleged`, `status`, `source_url`, `filing_date`) |
| `state_code` | text | no | 2-letter USPS (`TX`, `MN`, `DC`). Null when nationwide / ambiguous. Venue (USAO district) wins over defendant hometown. |
| `agency` | text | no | Canonical **victim** federal agency (SBA, HHS, DOL, USDA, …), not DOJ-as-prosecutor. Null if unmapped. |

Upsert remains `source_url`. When the same URL is re-posted, **update**
`state_code` and `agency` in place (backfill).

Indexes (add if missing):

```sql
CREATE INDEX idx_fc_state ON fraud_cases (state_code)
CREATE INDEX idx_fc_agency ON fraud_cases (agency)
```

## Public GET

`GET /hcgi/api/spending/fraud-cases`

Add filters (AND with existing `status`, `agency_program`, `q`, dates):

| Query | Matches |
|-------|---------|
| `state` | exact `state_code` (uppercase 2-letter). Omit = all states. |
| `agency` | case-insensitive contains on `agency`, **or** alias (`HHS` → Department of Health and Human Services, `SBA`, `DOL`, `USDA`, `VA`, `DOD`, `Treasury`, `IRS`, `HUD`, `SSA`, `ED`, `DHS`, `OPM`) |

Also return `state_code` and `agency` on every item (null allowed).

`GET /hcgi/api/spending/fraud-cases/states` (optional but useful):

```json
{ "ok": true, "items": [ { "state_code": "AL", "count": 3 }, ... ] }
```

Include all 50 states + `DC` even when `count` is 0. Do not hide empty states.

## Rules (unchanged)

- `source_url` required; upsert key. Hostname must be one of:
  `pandemicoversight.gov`, `pandemic.oversight.gov`, `oversight.gov`,
  `justice.gov`, `courtlistener.com`, `uscourts.gov` (and www/subdomains).
- `status` enum: `charged`, `convicted`, `settled`, `ongoing`
- `defendant_type` enum: `individual`, `company`, `multiple`
- `amount_alleged` nullable; never invent or take “over/more than” figures
- `filing_date` ISO `YYYY-MM-DD`
- `state_code` if present: `/^[A-Z]{2}$/`

## Fraud section UI

On the existing Fraud Cases page:

1. Show `state_code` and `agency` on every card (e.g. `MN · HHS · Medicaid`).
2. Filter bar: **State** (all 50 + DC, including zeros) and **Agency**
   (canonical names / aliases). Selecting a state hits `?state=MN`.
3. Empty state: “No federal cases tagged for this state yet” — do not
   invent rows.

Do not add a defendant-name search.

## Paste-ready Horizons prompt

```
Fraud cases already POST to /hcgi/api/ingest/spending/fraud-cases.
Extend the collection and public page for 50-state / all-agency coverage.

1) Schema: add optional fields on fraud_cases:
   - state_code text nullable (2-letter USPS; validate /^[A-Z]{2}$/ if present)
   - agency text nullable (canonical victim federal agency, e.g.
     "Department of Health and Human Services", "Small Business Administration")
   Indexes on state_code and agency.
   Upsert on source_url still; re-POST must update these two fields.

2) POST /hcgi/api/ingest/spending/fraud-cases
   Accept the new fields. Ignore unknown extras. Do not 400 if they are
   omitted (old batches). If state_code is present and not 2 letters, 400.

3) GET /hcgi/api/spending/fraud-cases
   Return state_code and agency on each item.
   New query filters (AND):
     state = exact state_code
     agency = case-insensitive contains on agency, or alias
       HHS / Health and Human Services → Department of Health and Human Services
       SBA → Small Business Administration
       DOL / Labor → Department of Labor
       USDA / Agriculture → Department of Agriculture
       VA → Department of Veterans Affairs
       DOD / Defense → Department of Defense
       Treasury / IRS → Department of the Treasury
       HUD → Department of Housing and Urban Development
       SSA → Social Security Administration
       ED / Education → Department of Education
       DHS → Department of Homeland Security
       OPM → Office of Personnel Management
   Keep existing status, agency_program, defendant_type, start/end, q.

4) Optional GET /hcgi/api/spending/fraud-cases/states
   Return all 50 states + DC with counts (0 if none). Do not omit zeros.

5) Fraud Cases UI
   Show state_code + agency on each card.
   Add State (all 50 + DC) and Agency filters that call the GET above.
   Empty filter result: "No federal cases tagged for this state yet."
   Do not invent cases. Do not add a defendant-name search.

This table names defendants — keep unpublished/review records out of
the public collection. Return { ok, written, updated } on POST.
```
