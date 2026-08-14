# rollcalltrack-ingest

External scheduled job that pulls U.S. House and Senate financial disclosure
filings (free/official sources), parses them, and posts them into the
rollcalltrack.com Horizons app via the ingest HTTP API. Horizons owns the
schema/UI; this repo is the scraper that keeps disclosures current.

## Why this is a separate project

Horizons builds the site (schema, API layer, UI). This repo scrapes House and
Senate disclosure feeds, parses PTRs, and writes through:

- `GET /hcgi/api/members/lookup` — multi-format name / bioguide match
- `POST /hcgi/api/ingest/disclosures` — idempotent filing upsert on `source_url`

Auth is a single header: `X-Ingest-Key`. No superuser password required.

## Setup

1. `pip install -r requirements.txt`
2. Copy `.env.example` to `.env` and set:
   - `HORIZONS_BASE_URL` — site origin, e.g.
     `https://<app-id>.app-preview.com`
   - `INGEST_API_KEY` — sent as `X-Ingest-Key` (from Horizons env vars)
3. Test parse-only (no API): `python -m ingest.main --source house --dry-run`
4. Test a real write once secrets are set (members must already exist in
   Horizons for name matching to succeed).

## Running on a schedule (free options)

### A) Windows Task Scheduler (recommended when GHA runners are stuck)

Runs on **this PC** — fully free, no hosted-runner minutes.

1. Copy `.env.example` → `.env` and set `HORIZONS_BASE_URL` + `INGEST_API_KEY`
2. Install the daily task (default **5:00 AM local**):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_windows_task.ps1
# optional:  ...\install_windows_task.ps1 -Hour 4 -Minute 30
```

3. Test once:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\run_daily_ingest.ps1
# or: schtasks /Run /TN "RollcalltrackDisclosureIngest"
```

Logs: `logs/latest.log` and `logs/ingest_*.log`  
Uninstall: `powershell -File scripts\uninstall_windows_task.ps1`  
Requires the PC to be **on** (or wake) at schedule time.

### B) GitHub Actions (public repo — free hosted minutes)

`.github/workflows/ingest.yml` runs daily at **09:00 UTC** + `workflow_dispatch`.

| Secret | Example |
|--------|---------|
| `HORIZONS_BASE_URL` | `https://…app-preview.com` |
| `INGEST_API_KEY` | Horizons ingest key |

```bash
gh workflow run "Disclosure Ingest"
```

If jobs stay **queued** with no runner, use option A until GitHub recovers.

Idempotency: re-POSTing the same `source_url` updates the existing filing
(`updated: true`) instead of creating duplicates.

## Live verification status (2026-08)

House and Senate paths were verified against the live government sites:

| Source | Status | Notes |
|--------|--------|-------|
| House bulk ZIP | Verified | `public_disc/financial-pdfs/{year}FD.zip` — index is XML (`{year}FD.xml`) |
| House PTR PDFs | Verified | URL is `public_disc/ptr-pdfs/{year}/{DocID}.pdf` (not financial-pdfs) |
| House PTR text parse | Verified | Recent e-filed PTRs have a text layer; OCR left as TODO |
| Senate agreement | Verified | `prohibition_agreement=1` + `csrfmiddlewaretoken` |
| Senate search API | Verified | POST `/search/report/data/` with `report_types='[11]'` (PTR) |
| Senate detail HTML | Verified | `table.table-striped` columns: date, owner, ticker, asset, type, amount |

Senate report type codes (form checkboxes): **7=Annual**, **11=PTR**,
10=Extension, 14=Blind Trusts, 15=Other. This pass ingests PTR only.

### Local dry-run (no DB required)

```bash
python -m ingest.main --source house --dry-run
python -m ingest.main --source senate --dry-run
# optional: widen window / raise cap
python -m ingest.main --source house --dry-run --days-back 60 --max-filings 10
```

### Member matching

Under Horizons API mode, member resolution hits
`GET /hcgi/api/members/lookup` (bioguide first, then multi-format name match:
raw → `Last, First` → compound last names). House passes state + district;
Senate passes state. Client-side name variants remain for the legacy
PocketBase path only.

```bash
# offline unit tests for name variants
python tests/test_names.py   # run from repo root with PYTHONPATH=.
```

## Folder structure

```
rollcalltrack-ingest/
  ingest/
    __init__.py
    config.py            # env var loading
    ingest_client.py     # Horizons HTTP API (lookup + filing upsert)
    db.py                # legacy PocketBase helpers
    house.py             # House bulk feed download + PDF parsing
    senate.py            # Senate eFD scrape + parsing
    usaspending.py       # USAspending.gov award search
    usaspending_agencies.json  # agency allowlist (edit to expand)
    gao.py               # GAO / PaymentAccuracy improper payments
    gao_reports.json     # FY → GAO report id/url/date catalog
    oversight.py         # Oversight.gov IG reports
    prac.py              # PRAC pandemic fraud enforcement
    prac_source_allowlist.json  # trusted source_url hosts
    congress_enrich.py   # Congress.gov member enrichment (legacy path)
    main.py              # orchestrator / CLI entry point
  .github/workflows/
    ingest.yml           # daily disclosure cron + workflow_dispatch
    usaspending.yml      # USAspending (schedule held)
    gao-improper-payments.yml  # GAO improper payments (dispatch + annual)
    oversight-ig.yml     # Oversight.gov IG reports (schedule held)
    prac-fraud.yml       # PRAC fraud cases (schedule held, dry-run default)
  requirements.txt
  .env.example
```

## USAspending awards (`usaspending-sync`)

Pulls federal prime awards from the public USAspending API
(`POST https://api.usaspending.gov/api/v2/search/spending_by_award/`)
and POSTs them in batches to `/hcgi/api/ingest/spending/awards`.

**This Horizons route is not deployed yet** (live `POST` returns 404). The
scraper and workflow are ready; do not enable unattended writes until
Horizons implements the endpoint. Paste `docs/HORIZONS_SPENDING_AWARDS.md`
into Horizons first.

### What it captures

| Field | Source |
|-------|--------|
| `award_id` | `generated_internal_id` (globally unique; used in USAspending URLs) |
| `recipient_name` | `Recipient Name` |
| `recipient_state` | `Recipient Location.state_code` (2-letter USPS only) |
| `awarding_agency` | `Awarding Agency` |
| `award_type` | mapped from type codes → `contract` / `grant` / `loan` / `direct_payment` / `insurance` |
| `amount` | `Award Amount` (official obligated figure) |
| `amount_type` | always `obligated` |
| `period_of_performance_start` / `_end` | `Start Date` / `End Date` (ISO `YYYY-MM-DD`) |
| `description` | `Description` |
| `source_url` | `https://www.usaspending.gov/award/{generated_internal_id}` |
| `fiscal_year` | the `--fiscal-year` you passed |

**Loans are not written as awards.** USAspending loans expose `Loan Value`
(face) and `Subsidy Cost`, not obligated `Award Amount`. Relabeling either
as `amount`/`obligated` would invent a figure. Those rows go to the
manual-review queue with the raw loan fields in the notes. Say if you want
a later pass that stores them as `amount_type: potential` (face) or a
separate subsidy field.

IDVs and “other” assistance (`11`) are out of the enum and are not requested.

### Agency allowlist

Default first-run set lives in `ingest/usaspending_agencies.json`:

`HHS`, `DOL`, `SBA`, `EPA`, `DOT`

Add a new agency by appending to `agencies` (abbrev + official toptier
`name` as USAspending lists it) and, if you want it in the daily job,
to `default_allowlist`. `--agency EPA` (or the full name) overrides the
list for a single run. Do not hardcode names in the request builder.

### CLI

```bash
# parse only, 5 records, default allowlist
python -m ingest.usaspending --fiscal-year 2026 --dry-run --max-records 5

# one agency, incremental window
python -m ingest.usaspending --fiscal-year 2026 --agency EPA --days-back 3 --max-records 20

# same via the shared orchestrator
python -m ingest.main --source usaspending --fiscal-year 2026 --dry-run --max-records 5
```

| Flag | Required | Notes |
|------|----------|--------|
| `--fiscal-year` | yes | Federal FY (FY2026 = 2025-10-01 … 2026-09-30) |
| `--agency` | no | Abbrev or full toptier name; default = allowlist |
| `--days-back` | no | `last_modified_date` window, clipped to the FY. Omit for the full FY. |
| `--max-records` | no | Hard cap across all agencies/types. Dry-run via `main.py` defaults to 5. |
| `--dry-run` | no | Search + map + log; no Horizons POST |

Actions workflow (`.github/workflows/usaspending.yml`) is **manual only**
until Horizons deploys `/hcgi/api/ingest/spending/awards`. Cron is commented
in the file; re-enable `schedule: cron: "0 9 * * *"` after that route exists.
`workflow_dispatch` still accepts FY / agency / days-back / max-records.
Same secrets as disclosures: `HORIZONS_BASE_URL` + `INGEST_API_KEY`
(`INGEST_KEY` also accepted).

Requests are paced (~1.5s between pages) with retry/backoff on 429/5xx.

### Sample payload (one award)

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

That contract row was checked live against the API in 2026-08. `amount` is
exactly the API’s `Award Amount` — not a ceiling, not a midpoint.

## GAO improper payments (`gao-improper-payments-sync`)

Annual / on-demand job. **Not a daily sync.**

GAO does not ship a program-level CSV. The FY 2025 report
([GAO-26-108694](https://www.gao.gov/products/gao-26-108694), 20-page PDF)
only tables programs ≥ $5B, and those tables do not extract cleanly. GAO’s
own analysis is of [PaymentAccuracy.gov](https://paymentaccuracy.gov/)
program pages, which publish official cause-level dollar amounts in HTML.
This job reads those published figures and attributes them to the GAO
report listed in `ingest/gao_reports.json`.

`POST /hcgi/api/ingest/spending/improper-payments` is **not deployed**
(live 404). See `docs/HORIZONS_IMPROPER_PAYMENTS.md`. Use `--dry-run`
until that route exists.

### What it captures

| Field | Source |
|-------|--------|
| `agency` | “Sponsoring agency” on the PaymentAccuracy program page |
| `program_name` | Program H1 / breadcrumb |
| `fiscal_year` | `--fiscal-year` |
| `estimated_amount_low` / `_high` | Published cause dollars (same value when the source gives one figure) |
| `cause_category` | Mapped from published labels (below) |
| `source_report_title` / `source_url` / `report_date` | `ingest/gao_reports.json` for that FY |

Cause mapping (label fragment → enum):

| PaymentAccuracy label | `cause_category` |
|-----------------------|------------------|
| Amount of overpayments within/outside agency control | `overpayment` |
| Amount of underpayments | `underpayment` |
| Technically improper (right recipient, wrong procedure) | `documentation_error` |
| Unknown / insufficient documentation | `other` |
| Explicit “fraud” dollar line only | `fraud` |

We **do not** set `amount = outlays × improper-payment rate`. We **do not**
label overpayments as fraud. Zero-dollar cause lines are skipped. Within +
outside overpayment lines are summed — those are two published parts of the
same official overpayment total, not a midpoint.

Dollars on the site are printed as `$35,029.54 M` (millions). Stored values
are full dollars (`35029540000.0`).

### CLI

```bash
python -m ingest.gao --fiscal-year 2025 --dry-run --max-records 3
python -m ingest.main --source gao --fiscal-year 2025 --dry-run --max-records 3
```

| Flag | Required | Notes |
|------|----------|--------|
| `--fiscal-year` | yes | Must exist in `ingest/gao_reports.json` |
| `--dry-run` | no | Parse + log; no POST. Prefer this until Horizons exists. |
| `--max-records` | no | Cap program pages. `main.py --dry-run` defaults to 3. |

Workflow: `.github/workflows/gao-improper-payments.yml` — **dispatch is
primary** (`dry_run` defaults to true). Annual cron May 15 12:00 UTC is a
backup only.

Add a new year by appending to `ingest/gao_reports.json` (report id, title,
product URL, ISO `report_date`). Do not invent a GAO URL.

### Sample payload

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

`$35,029.54 M` overpayments on the Medicaid FY 2025 PaymentAccuracy page
matches GAO’s published ~$37.4B Medicaid headline once underpayment and
technically-improper lines are included as separate records.

### If GAO / PaymentAccuracy change format

This is the most parsing-fragile job. Expect to touch:

1. **`ingest/gao_reports.json`** — every new FY. If you skip this, the job
   refuses to run rather than guessing a report URL.
2. **`CAUSE_LABELS` in `ingest/gao.py`** — if PaymentAccuracy renames
   “Amount of overpayments outside the agency's control” etc.
3. **`[data-year="{fy}"]` wrappers** — if they drop `data-year` or split
   years another way, `parse_program_page` must change.
4. **`$35,029.54 M` money format** — `parse_money_to_dollars`. A switch to
   full dollars or `$35.0 billion` is handled; a new unit is not.
5. **Program index** — `/agencies-and-programs` links to `/program/...`.
   If that index dies, fall back to the sitemap `/program/` list.
6. **Excel download** — `downloadDataset()` on PaymentAccuracy looks for
   `/assets/files/{data-file-name}`, but those filenames are not in the
   sitemap and the site 200s HTML for missing paths. If they publish a
   stable xlsx, prefer that over HTML and delete most of the page parser.
7. **Do not fall back to the GAO PDF** without a new, deliberate parser.
   Table extraction on GAO-26-108694 is broken (split headers, wrapped
   agency/program names, incomplete program list). That is not worth
   over-engineering for an annual file.

## Oversight.gov IG reports (`oversight-ig-sync`)

Weekly job. Pulls Inspector General report metadata from
[Oversight.gov](https://www.oversight.gov/reports/federal).

**No public reports API.** Checked 2026-08: `/jsonapi`, `/api/reports`,
`?_format=json` are 404/406. Site RSS is homepage news, not reports. The
job uses the public Drupal listing + report pages.

`robots.txt`: `Crawl-delay: 5`. `/search/` is disallowed — we never hit it.
Only `/reports/federal` (date/agency filters) and individual `/reports/...`
pages.

`POST /hcgi/api/ingest/spending/ig-reports` is **not deployed** (live 404).
See `docs/HORIZONS_IG_REPORTS.md`. Dispatch `dry_run` defaults to true.

### What it captures

| Field | Source |
|-------|--------|
| `agency` | Agency Reviewed / Investigated |
| `report_title` | Report title |
| `report_type` | Mapped from Type (below) |
| `financial_impact` | Explicit “Questioned Costs” or “Funds for Better Use” only |
| `summary` | First 2–3 sentences of the report body (or recommendation text) |
| `recommendations_count` | Number of Recommendations field |
| `source_url` | Canonical Oversight.gov report URL |
| `publish_date` | Date Issued (`YYYY-MM-DD`) |

Type mapping:

| Oversight.gov Type | enum |
|--------------------|------|
| Audit | `audit` |
| Investigation | `investigation` |
| Inspection / Evaluation | `evaluation` |
| Semiannual Report | `semiannual_report` |
| Other, Review, Peer Review, CIGIE Annual, Disaster Recovery, Top Management Challenges | **manual_review** — not guessed |

`financial_impact`: populated only when the page states a dollar figure.
If questioned costs and funds-for-better-use are both present and **differ**,
the field is left null (we do not add them). `$0` is an explicit figure and
is stored as `0.0`. We never parse “millions” out of prose.

### CLI

```bash
python -m ingest.oversight --days-back 14 --dry-run --max-records 3
python -m ingest.oversight --days-back 30 --agency EPA --max-records 10 --dry-run
python -m ingest.main --source oversight --days-back 14 --dry-run --max-records 3
```

| Flag | Required | Notes |
|------|----------|--------|
| `--days-back` | no | Default 14. Filters Date Issued min/max. |
| `--agency` | no | Substring or exact agency name from the listing filter. |
| `--max-records` | no | Cap reports. `main.py --dry-run` defaults to 3. |
| `--dry-run` | no | Parse + log; no POST. |

Workflow: `.github/workflows/oversight-ig.yml` is **manual only** until
Horizons deploys `/hcgi/api/ingest/spending/ig-reports`. Weekly cron is
commented in the file (`0 12 * * 1`). Dispatch `dry_run` defaults to true.
Requests are paced at 5 seconds.

### Sample payload

```json
{
  "ig_reports": [
    {
      "agency": "General Services Administration",
      "report_title": "The Processes FAS Uses to Ensure It Awards and Collects Accurate Product Data Are Ineffective",
      "report_type": "audit",
      "financial_impact": 0.0,
      "summary": "FAS catalog data contains inconsistent manufacturer part numbers. Identical products cannot be compared reliably across systems. The OIG issued recommendations to the FAS Commissioner.",
      "recommendations_count": 6,
      "source_url": "https://www.oversight.gov/reports/audit/processes-fas-uses-ensure-it-awards-and-collects-accurate-product-data-are",
      "publish_date": "2026-08-13"
    }
  ]
}
```

## PRAC fraud cases (`prac-fraud-sync`)

Highest-sensitivity job in this repo. It names defendants. **Dry-run is
the default.** We post only when you pass `--post`, and only records whose
`source_url` is on a trusted-domain allowlist.

**No PRAC enforcement CSV/API** (checked 2026-08). “Download the Data” is
PPP/UI spending files, not case lists. Official source is PRAC’s
[Investigative Results](https://www.pandemicoversight.gov/oversight/investigative-results)
listing (`/oversight/reports?f[0]=report_type_taxonomy:85`), which links to
DOJ press releases. `/search/` is robots-disallowed and is not used.

`POST /hcgi/api/ingest/spending/fraud-cases` is **not deployed** (live 404).
See `docs/HORIZONS_FRAUD_CASES.md`.

### What it captures

| Field | Source |
|-------|--------|
| `case_title` | Listing title |
| `agency_program` | Mapped only if title/body names a known pandemic program |
| `defendant_type` | `individual` / `company` / `multiple` from explicit wording |
| `amount_alleged` | Unhedged explicit `$X` / `$X million` only |
| `status` | `charged` / `convicted` / `settled` / `ongoing` from wording |
| `source_url` | The listing’s outbound link (must be allowlisted) |
| `filing_date` | Listing `<time datetime>` |

**Allowlist** (`ingest/prac_source_allowlist.json`):
`pandemicoversight.gov`, `pandemic.oversight.gov`, `oversight.gov`,
`justice.gov`, `courtlistener.com`, `uscourts.gov` (and `www`/subdomains).

Anything else — news aggregators included — goes to **manual_review**, not
to the public table.

Also reviewed (not auto-posted) when we cannot map program, status, or
defendant type. Example: a dogfighting sentencing that appeared on the
PRAC listing has no pandemic program → review.

Hedged dollars (`over $40,000`, `more than $1 million`) are stored as
null, not as a guessed floor.

### Monitor the review rate

If most records land in `manual_review`, **tune the allowlist or the
program/status maps — do not casually loosen the domain list.** A bad
source on this table is a defamation problem. Check logs for
`source host … not on allowlist` vs `could not confidently map`. Only add
a domain if it is a primary PRAC/DOJ/court record, never a recap site.

### CLI

```bash
python -m ingest.prac --days-back 14 --max-records 10          # dry-run (default)
python -m ingest.prac --days-back 14 --max-records 10 --post   # write (after Horizons exists)
python -m ingest.main --source prac --days-back 14             # always dry-run
```

| Flag | Required | Notes |
|------|----------|--------|
| `--days-back` | no | Default 14. Stops when listing dates are older. |
| `--max-records` | no | Cap rows. |
| `--dry-run` | no | **Default on.** |
| `--post` | no | Only way to POST from this job. |

Workflow: `.github/workflows/prac-fraud.yml` is **manual only** (weekly
cron held). Dispatch `dry_run` defaults to true.

### Sample payload

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

`$3.9 million` is the figure in the PRAC listing and the DOJ title — not
computed. The same listing’s “over $40,000” PUA case would have
`amount_alleged: null`.
