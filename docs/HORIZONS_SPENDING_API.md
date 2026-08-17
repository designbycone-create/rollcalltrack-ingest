# Horizons spending API + PocketBase collections

Auth for all ingest routes: header `X-Ingest-Key` (same secret as
`POST /hcgi/api/ingest/disclosures`). Public GET routes need no key.

Create/update/delete on these collections stay **API-only**
(`createRule`/`updateRule`/`deleteRule` = null). List/view are public
(`""`) except `manual_review_queue`, which stays admin-only.

Workers already POST the batch shapes below. Support those **and** the
disclosures-style `{ "type", "payload" }` wrapper so single-record and
batch both work.

Do **not** invent or average dollar amounts. If a range is supplied,
store `*_low` / `*_high` only.

Live status (2026-08-14): all four ingest routes 404. Implement this
before turning ingest `--post` on.

---

## 1. PocketBase collections

Every collection includes PocketBase `id`, `created`, `updated`.

Dates are **ISO `YYYY-MM-DD` text** (same as `disclosure_filings.filing_date`),
not datetime, unless noted.

### 1.1 `spending_awards`

USAspending prime awards. Worker: `python -m ingest.usaspending`.

| Field | Type | Required | Notes |
|-------|------|----------|--------|
| `award_id` | text | yes | USAspending `generated_internal_id`. Unique. |
| `recipient_name` | text | yes | |
| `recipient_state` | text | no | 2-letter USPS or empty. Reject other lengths. |
| `awarding_agency` | text | yes | Toptier agency name. |
| `award_type` | select (1) | federal yes | `contract`, `grant`, `loan`, `direct_payment`, `insurance`. Null ok when `jurisdiction=state`. |
| `amount` | number | yes | Federal: official obligated (or potential). State: portal posted expenditure. Never a midpoint. |
| `amount_type` | select (1) | federal yes | `obligated`, `potential`. Null ok when `jurisdiction=state`. |
| `jurisdiction` | select (1) | no | `federal` (default) or `state` |
| `state_code` | text | if state | 2-letter USPS. Null for federal. |
| `program` | text | no | State program label when the portal has one. |
| `period_of_performance_start` | text | yes | `YYYY-MM-DD` |
| `period_of_performance_end` | text | no | `YYYY-MM-DD` or empty |
| `description` | text (editor ok) | no | |
| `source_url` | url/text | yes | Unique upsert key. USAspending award page. |
| `fiscal_year` | number (int) | yes | Federal FY |

Indexes:

```sql
CREATE UNIQUE INDEX idx_sa_source_url ON spending_awards (source_url)
CREATE UNIQUE INDEX idx_sa_award_id ON spending_awards (award_id)
CREATE INDEX idx_sa_agency ON spending_awards (awarding_agency)
CREATE INDEX idx_sa_state ON spending_awards (recipient_state)
CREATE INDEX idx_sa_fy ON spending_awards (fiscal_year)
CREATE INDEX idx_sa_type ON spending_awards (award_type)
CREATE INDEX idx_sa_jurisdiction ON spending_awards (jurisdiction)
CREATE INDEX idx_sa_state_code ON spending_awards (state_code)
```

### 1.2 `improper_payments`

GAO / PaymentAccuracy program-cause rows. Worker: `python -m ingest.gao`.

`source_url` is the **GAO report** URL and is **not unique** (one report,
many programs/causes).

| Field | Type | Required | Notes |
|-------|------|----------|--------|
| `agency` | text | yes | Sponsoring agency. |
| `program_name` | text | yes | |
| `fiscal_year` | number (int) | yes | |
| `estimated_amount_low` | number | yes | Published dollars. |
| `estimated_amount_high` | number | no | Same as low when the source is a single figure. Null only if the source is explicitly open-ended. |
| `cause_category` | select (1) | yes | `overpayment`, `underpayment`, `fraud`, `documentation_error`, `other` |
| `source_report_title` | text | yes | |
| `source_url` | url/text | yes | GAO product URL. |
| `report_date` | text | yes | `YYYY-MM-DD` |

Indexes:

```sql
CREATE UNIQUE INDEX idx_ip_upsert ON improper_payments (source_url, fiscal_year, program_name, cause_category)
CREATE INDEX idx_ip_agency ON improper_payments (agency)
CREATE INDEX idx_ip_fy ON improper_payments (fiscal_year)
CREATE INDEX idx_ip_cause ON improper_payments (cause_category)
```

### 1.3 `ig_reports`

Oversight.gov IG reports. Worker: `python -m ingest.oversight`.

| Field | Type | Required | Notes |
|-------|------|----------|--------|
| `agency` | text | yes | Agency reviewed. |
| `report_title` | text | yes | |
| `report_type` | select (1) | yes | `audit`, `investigation`, `evaluation`, `semiannual_report` |
| `financial_impact` | number | no | Explicit questioned costs / funds-for-better-use only. Null if two different figures were published (do not add them). |
| `summary` | text | no | 2–3 factual sentences. |
| `recommendations_count` | number (int) | no | |
| `source_url` | url/text | yes | Unique upsert key. Oversight.gov report URL. |
| `publish_date` | text | yes | `YYYY-MM-DD` |

Indexes:

```sql
CREATE UNIQUE INDEX idx_ig_source_url ON ig_reports (source_url)
CREATE INDEX idx_ig_agency ON ig_reports (agency)
CREATE INDEX idx_ig_type ON ig_reports (report_type)
CREATE INDEX idx_ig_publish ON ig_reports (publish_date)
```

### 1.4 `fraud_cases`

PRAC Investigative Results (usually DOJ PRs). Worker: `python -m ingest.prac`.

This collection **names defendants**. Public list is intended, but ingest
must reject untrusted `source_url` hosts (see §2.5).

| Field | Type | Required | Notes |
|-------|------|----------|--------|
| `case_title` | text | yes | |
| `agency_program` | text | yes | Named program (e.g. PPP), not a guessed agency. |
| `agency` | text | no | Canonical **victim** federal agency. Null if unmapped. |
| `state_code` | text | no | 2-letter USPS venue. Null if nationwide / ambiguous. |
| `defendant_type` | select (1) | yes | `individual`, `company`, `multiple` |
| `amount_alleged` | number | no | Unhedged explicit `$` only. |
| `status` | select (1) | yes | `charged`, `convicted`, `settled`, `ongoing` |
| `source_url` | url/text | yes | Unique upsert key. Allowlisted host only. |
| `filing_date` | text | yes | `YYYY-MM-DD` |

Indexes:

```sql
CREATE UNIQUE INDEX idx_fc_source_url ON fraud_cases (source_url)
CREATE INDEX idx_fc_program ON fraud_cases (agency_program)
CREATE INDEX idx_fc_agency ON fraud_cases (agency)
CREATE INDEX idx_fc_state ON fraud_cases (state_code)
CREATE INDEX idx_fc_status ON fraud_cases (status)
CREATE INDEX idx_fc_filing ON fraud_cases (filing_date)
```

Server-side allowlist (must match `ingest/prac_source_allowlist.json`):

- `pandemicoversight.gov`
- `pandemic.oversight.gov`
- `oversight.gov`
- `justice.gov`
- `courtlistener.com`
- `uscourts.gov`

Allow `www.` and subdomains (`usao-ma.justice.gov` is **not** a host;
real hosts look like `www.justice.gov`). Match hostname == domain or
hostname ends with `.` + domain.

### 1.5 Extend `manual_review_queue`

Keep the existing collection. Add three optional fields so spending
workers can flag rows without pretending they are filings:

| Field | Type | Required | Notes |
|-------|------|----------|--------|
| `collection` | select (1) | no | `disclosure_filings`, `spending_awards`, `improper_payments`, `ig_reports`, `fraud_cases` |
| `source_url` | url/text | no | |
| `payload` | json | no | Raw rejected record for an admin. |

`filing_id` stays for the disclosure path. Spending review items may set
`filing_id` to the `source_url` or `"unknown"` (workers already do this).

Add reason values (keep existing ones):

- `untrusted_source`
- `unmapped_enum`
- `missing_source_url`
- `unexpected_structure` (already used)
- `ambiguous_amount`

---

## 2. Ingest endpoints

All: `POST`, `Content-Type: application/json`, `X-Ingest-Key`.

Missing/invalid key → `401`. Validation failure → `400` with
`{ "ok": false, "errors": [ { "index", "field", "message" } ] }`.
Do not 500 on bad enums.

Idempotent upsert: same unique key → update in place, same `id`,
`updated: true`.

Each route accepts:

1. **Batch** — array at the resource key (what the Python workers send).
2. **Single** — one object at `payload`, or `{ "type", "payload" }`.
3. **Review** — `{ "type": "review_queue", "payload": { ... } }` on any of
   these routes (or keep posting reviews to `/ingest/disclosures`).

### 2.1 `POST /hcgi/api/ingest/spending/awards`

```json
{ "awards": [ { "award_id", "recipient_name", "recipient_state", "awarding_agency",
  "award_type", "amount", "amount_type", "period_of_performance_start",
  "period_of_performance_end", "description", "source_url", "fiscal_year" } ] }
```

or `{ "type": "award", "payload": { ...one award... } }`.

Upsert on `source_url`. Also reject duplicate `award_id` on create.

Response:

```json
{ "ok": true, "written": 2, "updated": 1, "ids": ["...", "..."] }
```

### 2.2 `POST /hcgi/api/ingest/spending/improper-payments`

```json
{ "improper_payments": [ { "agency", "program_name", "fiscal_year",
  "estimated_amount_low", "estimated_amount_high", "cause_category",
  "source_report_title", "source_url", "report_date" } ] }
```

or `{ "type": "improper_payment", "payload": { ... } }`.

Upsert on `(source_url, fiscal_year, program_name, cause_category)`.

If `estimated_amount_high` is omitted and `estimated_amount_low` is set,
store high = low (single published figure). If the client sends
`estimated_amount_high: null` **and** a note that the source is
open-ended, keep high null. Never average low/high into a third field.

### 2.3 `POST /hcgi/api/ingest/spending/ig-reports`

```json
{ "ig_reports": [ { "agency", "report_title", "report_type",
  "financial_impact", "summary", "recommendations_count",
  "source_url", "publish_date" } ] }
```

or `{ "type": "ig_report", "payload": { ... } }`.

Upsert on `source_url`.

### 2.4 `POST /hcgi/api/ingest/spending/fraud-cases`

```json
{ "fraud_cases": [ { "case_title", "agency_program", "agency", "state_code",
  "defendant_type", "amount_alleged", "status", "source_url", "filing_date" } ] }
```

or `{ "type": "fraud_case", "payload": { ... } }`.

Upsert on `source_url`.

**Server must re-check the allowlist.** If the host is not trusted,
do **not** write `fraud_cases`. Write `manual_review_queue` with
`reason: "untrusted_source"` and return

```json
{ "ok": true, "written": 0, "updated": 0, "reviewed": 1 }
```

for that item (batch continues). Never auto-publish a bad source.

### 2.5 Shared validation

| Check | Rule |
|-------|------|
| `source_url` | Required, http(s), non-empty. |
| Dates | `YYYY-MM-DD` only. |
| Selects | Exact enum; 400 otherwise (no silent coerce except the documented disclosure maps). |
| Numbers | Finite numbers. Reject strings like `"about 3 million"`. |
| `recipient_state` | empty or `/^[A-Z]{2}$/` after uppercasing. |
| Fraud host | Allowlist (§1.4). |

---

## 3. Read endpoints

Public. Paginate: `page` (default 1), `limit` (default 25, max 100).

Response envelope:

```json
{
  "ok": true,
  "page": 1,
  "limit": 25,
  "total": 0,
  "items": []
}
```

### 3.1 `GET /hcgi/api/spending/awards`

Filters (AND):

| Query | Matches |
|-------|---------|
| `agency` | `awarding_agency` contains (case-insensitive) |
| `state` | `recipient_state` exact (2-letter) |
| `fiscal_year` | exact int |
| `award_type` | exact enum |
| `amount_type` | exact enum |
| `q` | contains on `recipient_name`, `award_id`, `description` |
| `start` / `end` | `period_of_performance_start` range (`YYYY-MM-DD`) |

Sort: `fiscal_year` DESC, `amount` DESC.

### 3.2 `GET /hcgi/api/spending/improper-payments`

| Query | Matches |
|-------|---------|
| `agency` | `agency` contains |
| `fiscal_year` | exact |
| `cause_category` | exact enum |
| `program` | `program_name` contains |
| `start` / `end` | `report_date` range |

Sort: `fiscal_year` DESC, `estimated_amount_low` DESC.

### 3.3 `GET /hcgi/api/spending/ig-reports`

| Query | Matches |
|-------|---------|
| `agency` | `agency` contains |
| `report_type` | exact enum |
| `start` / `end` | `publish_date` range |
| `q` | contains on `report_title`, `summary` |

Sort: `publish_date` DESC.

There is no open/closed field. “Open” in the rollup (§4) means
“published in the selected window,” not a workflow status.

### 3.4 `GET /hcgi/api/spending/fraud-cases`

| Query | Matches |
|-------|---------|
| `state` | exact `state_code` (2-letter) |
| `agency` | `agency` contains, or alias (HHS/SBA/DOL/…); fallback: program→agency map in §4 |
| `agency_program` | exact/contains on `agency_program` |
| `status` | exact enum |
| `defendant_type` | exact enum |
| `start` / `end` | `filing_date` range |
| `q` | contains on `case_title` |

Sort: `filing_date` DESC.

Do not add a public filter that is just a defendant name. Search is
title-level only.

---

## 4. Agency rollup

`GET /hcgi/api/spending/agency-summary/:agency`

`:agency` is a slug or display name (`epa`, `EPA`,
`Environmental Protection Agency`). Resolve with the alias table below
(extend as needed; do not fuzzy-match defendant names).

Optional query: `fiscal_year` (applies to awards + improper payments),
`ig_since` (default: 24 months ago, ISO date — used as `publish_date >=`),
`fraud_since` (default: same 24 months, `filing_date >=`).

```json
{
  "ok": true,
  "agency": "Environmental Protection Agency",
  "matched_aliases": ["EPA", "Environmental Protection Agency"],
  "fiscal_year": null,
  "total_obligated": 0,
  "award_count": 0,
  "total_improper_payments": 0,
  "improper_payment_count": 0,
  "ig_report_count": 0,
  "fraud_case_count": 0
}
```

Definitions:

| Metric | How |
|--------|-----|
| `total_obligated` | `SUM(amount)` on `spending_awards` where `amount_type = obligated` and `awarding_agency` matches an alias. |
| `award_count` | Count of those award rows. |
| `total_improper_payments` | `SUM(estimated_amount_low)` on `improper_payments` where `agency` matches. Use low, not a midpoint. |
| `improper_payment_count` | Count of those rows. |
| `ig_report_count` | Count of `ig_reports` where `agency` matches and `publish_date >= ig_since`. Not an “open ticket” count. |
| `fraud_case_count` | Count of `fraud_cases` where `agency` matches an alias, or (if `agency` is null) `agency_program` maps via the table below, and `filing_date >= fraud_since`. |

**Program → agency** (fallback when `agency` is null):

| `agency_program` contains | Agency |
|---------------------------|--------|
| Paycheck Protection / PPP | Small Business Administration |
| EIDL | Small Business Administration |
| Shuttered Venue | Small Business Administration |
| Restaurant Revitalization | Small Business Administration |
| Medicaid / Medicare / Health Care Fraud / Provider Relief | Department of Health and Human Services |
| Child Nutrition / SNAP / WIC | Department of Agriculture |
| Pandemic Unemployment / PUA / FPUC / Unemployment Insurance | Department of Labor |
| Employee Retention Credit / ERC | Department of the Treasury |
| COVID Rental Assistance | Department of the Treasury / Treasury ERA (label: `Department of the Treasury`) |
| Economic Impact Payments | Department of the Treasury |
| TRICARE | Department of Defense |
| VA Health / CHAMPVA | Department of Veterans Affairs |

If `:agency` matches none of the aliases, return `404`
`{ "ok": false, "error": "unknown agency" }`.

Starter aliases:

| Canonical name | Also match |
|----------------|------------|
| Department of Health and Human Services | HHS, Health and Human Services |
| Department of Labor | DOL, Labor |
| Small Business Administration | SBA |
| Environmental Protection Agency | EPA |
| Department of Transportation | DOT, Transportation |
| Department of Justice | DOJ, Justice |
| Department of the Treasury | Treasury, IRS |
| Department of Housing and Urban Development | HUD |
| General Services Administration | GSA |
| Department of Agriculture | USDA, Agriculture |
| Department of Education | ED, Education |
| Social Security Administration | SSA |
| Department of Veterans Affairs | VA, Veterans Affairs |
| Department of Homeland Security | DHS, Homeland Security |
| Department of War | DOD, Defense, Department of Defense |

---

## 5. Paste-ready Horizons prompt

```
Implement PocketBase collections + /hcgi/api spending routes per
docs/HORIZONS_SPENDING_API.md in the rollcalltrack-ingest repo.

Collections: spending_awards, improper_payments, ig_reports, fraud_cases.
Extend manual_review_queue with optional collection, source_url, payload.

Ingest (X-Ingest-Key, same as /hcgi/api/ingest/disclosures):
POST /hcgi/api/ingest/spending/awards
POST /hcgi/api/ingest/spending/improper-payments
POST /hcgi/api/ingest/spending/ig-reports
POST /hcgi/api/ingest/spending/fraud-cases
Batch arrays AND single {type,payload}. Upsert as specified.
400 + field errors on bad enums/dates. Never invent dollar midpoints.
fraud_cases: reject non-allowlisted source_url into manual_review_queue
(reason untrusted_source), do not insert the case.

Public GET:
/hcgi/api/spending/awards
/hcgi/api/spending/improper-payments
/hcgi/api/spending/ig-reports
/hcgi/api/spending/fraud-cases
Filters: agency, state, fiscal_year, date start/end as in the doc.
Pagination page/limit.

GET /hcgi/api/spending/agency-summary/:agency
returns total_obligated, award_count, total_improper_payments,
improper_payment_count, ig_report_count, fraud_case_count
using the alias + program→agency maps in the doc.

listRule/viewRule public on the four collections; writes API-only.
```
