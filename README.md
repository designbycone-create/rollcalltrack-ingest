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
4. A real write requires `--live` or `INGEST_LIVE=1` in addition to the
   secrets (members must already exist in Horizons for name matching to
   succeed). Pytest never posts to that endpoint: tests stay dry-run even
   if the variable is set in the environment.

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

`INGEST_KEY` is still read for one release when `INGEST_API_KEY` is unset.
Workflows set `INGEST_API_KEY` only.

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
| House PTR text parse | Verified | Text layer first. If that yields no transactions, OCR with tesseract + poppler (`parse_status` stays `failed` and records a reason when OCR also finds nothing) |
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

### Re-ingest one House filing

Each parsed transaction has a `line_index`: its 0-based position in that
filing. Identical trades (same asset, date, and amount) stay separate rows.
Stored rows that were collapsed before this, or that have a null ticker, can
be re-posted by House DocID. `--filing-id` ignores `--days-back` and
`--max-filings`. Pass `--year` when the DocID is not in the current year.
The flag is only valid with `--source house`.

```bash
python -m ingest.main --source house --dry-run --filing-id 20035588 --year 2026
python -m ingest.main --source house --live --filing-id 20035588
python -m ingest.main --source house --live --filing-id 20035588 --year 2026
```

`--filing-id` also re-parses a scanned PTR. The text layer is tried first.
When it contains no transactions, the PDF is OCR'd (tesseract + poppler).
If OCR still finds no rows, the filing is stored with `parse_status=failed`
and `parse_failure_reason` (`ocr_failed: no text extracted` or
`ocr_failed: no transactions recognized`).

Kevin Hern's PTR `20035588` is the case this fixes: 24 transaction lines,
including two identical Lockheed Martin partial sales (source lines 47 and
51), of which only 23 were stored.

### Backfill member links

Filings already in Horizons can lack `member_id` or `member_party` because
the lookup was given a 2-letter state or a Clerk name like
`Richard Dean Dr McCormick`. The backfill lists those filings and their
transactions, resolves them, and prints every change. It does not POST
unless `--live` is set, and it never writes a blank over a value that is
already stored. The disclosures write replaces a filing's transactions, so
a live run sends that filing's full transaction list back with `line_index`,
`ticker`, and `ticker_confidence` unchanged and the filled member id. A
bioguide is not sent as `member_id` (that value returns HTTP 500, and the
writer then stores the filing with no member link). When no transactions
were listed, the live run does not POST, because an empty list would drop
trades.

Filings are read from `GET /hcgi/api/members` (paged) and
`GET /hcgi/api/members/{id}/disclosures`, which returns that member's
filings and transactions (`line_index`, `ticker`, `ticker_confidence`)
side by side. `GET /hcgi/api/filings`, `GET /hcgi/api/disclosures/filings`,
and `GET /hcgi/api/transactions` are not deployed. `GET /hcgi/api/disclosures/recent`
is a capped transaction feed and is not the filing index. A filing with no
`member_id` is absent from every member's disclosures list. No working
single-filing GET was found. `GET /hcgi/api/ingest/disclosures/{id}` and
the sibling ingest paths answer `{"error":"Route not found"}`. The public
candidates `GET /hcgi/api/disclosures/{id}`,
`GET /hcgi/api/disclosures/filings/{id}`,
`GET /hcgi/api/disclosures/filing/{id}`, `GET /hcgi/api/filings/{id}`, and
`GET /hcgi/api/filing/{id}` were not confirmed against the origin from this
network (Cloudflare returned 403 HTML). When the origin was reachable,
`GET /hcgi/api/filings` and `GET /hcgi/api/disclosures/filings` (no id)
returned application 404. `--relink-filing-ids` tries the five public
paths, prints the planned link when one returns the filing, and exits
non-zero when every response is an application 404 ("No single-filing GET
route is deployed") or a Cloudflare 403 ("did not reach the app"). Name
matching is the current lookup (McConnell middle-name/nickname, and a
state taken from the office line).
Live mode sends the update through the existing disclosures ingest POST,
including the filing's transactions. If the members
route does not answer, or a member disclosure stays rate-limited or
unavailable, the command exits non-zero instead of reporting zero
changes or a partial list. The public read API allows 100 requests per
300 seconds (`ratelimit-policy: 100;w=300`). The client honors
`ratelimit-remaining` and waits out `ratelimit-reset` before that window
is spent. Reads do not need `INGEST_API_KEY`.

```bash
python -m ingest.backfill_members
python -m ingest.backfill_members --live
python -m ingest.backfill_members --relink-filing-ids vfjvcedtqldoxxs
```

Both commands need `HORIZONS_BASE_URL` and `INGEST_API_KEY`. The dry run
only reads. `--live` also sets the write opt-in for that process.

The same commands run from Actions → **Backfill members** (manual only).
`mode` defaults to `dry-run`; `live` adds `--live`. Optional `filing_ids`
(comma-separated House DocIDs) re-parses each one with
`python -m ingest.main --source house --filing-id …` in that same mode.
Optional `relink_filing_ids` (comma-separated Horizons filing ids) skips
the member catalog and tries the single-filing GET routes above. The job
uploads `planned-changes` and writes a run summary.

### Member matching

Under Horizons API mode, member resolution hits
`GET /hcgi/api/members/lookup`. USPS codes, including DC and the
territories, are sent as full state names (`OH` → `Ohio`, `CA` →
`California`). Names are normalized first: titles and suffixes
(`Dr`, `Hon`, `Jr`, `III`) and middle names are removed. The attempts
are the full name plus state, then the last name plus state, then the
bioguide id when the filing or the congress-legislators roster has one.
A roster row is not submitted as a member id.

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
    db.py                # PocketBase helpers; ingest auth is the API key
    house.py             # House bulk feed download + PDF parsing
    senate.py            # Senate eFD scrape + parsing
    senate_votes.py      # Senate LIS roll calls → /ingest/votes
    usaspending.py       # USAspending.gov award search
    usaspending_agencies.json  # agency allowlist (edit to expand)
    gao.py               # GAO / PaymentAccuracy improper payments
    gao_reports.json     # FY → GAO report id/url/date catalog
    oversight.py         # Oversight.gov IG reports
    prac.py              # PRAC pandemic fraud enforcement
    prac_source_allowlist.json  # trusted source_url hosts
    fjc.py               # FJC Article III biographical directory
    judicial_events.py   # opinion text → scored event or review
    judicial_score.py    # law-over-politics + ideology formula
    jcs_scores.py        # JCS / Martin-Quinn snapshot loader
    courtlistener_events.py  # CourtListener opinion → events
    scotus_stays.py      # SCOTUS stay/reversal → events
    jefs.py              # AO / JEFS judicial financial disclosures
    nominations.py       # Congress.gov PN → FJC judge + Senate vote
    legiscan.py          # LegiScan 50-state legislator roster
    legiscan_bills.py    # LegiScan state bills + roll calls + coverage
    ca_spending.py       # California Open FI$Cal vendor expenditures
    state_spending.py    # 50-state spending orchestrator
    state_portals.json   # official checkbook catalog + adapters
    congress_enrich.py   # Congress.gov member enrichment (legacy path)
    main.py              # orchestrator / CLI entry point
  .github/workflows/
    ingest.yml           # daily disclosures + Senate roll calls
    usaspending.yml      # USAspending (schedule held)
    ca-spending.yml      # CA Open FI$Cal (schedule held, monthly)
    state-spending.yml   # 50-state spending (schedule held, monthly)
    gao-improper-payments.yml  # GAO improper payments (dispatch + annual)
    oversight-ig.yml     # Oversight.gov IG reports (schedule held)
    prac-fraud.yml       # PRAC fraud cases (schedule held, dry-run default)
    fjc-judges.yml       # FJC judges (dispatch primary, quarterly cron)
    courtlistener-events.yml  # CourtListener events (schedule held)
    scotus-stays.yml     # SCOTUS stays/reversals (schedule held)
    jcs-scores.yml       # JCS snapshot (dispatch, dry-run default)
    judicial-score.yml   # score job (schedule held)
    jefs-disclosures.yml # AO judicial disclosures (dispatch + weekly cron)
    judicial-nominations.yml  # PN → judge + vote (dispatch + weekly cron)
    legiscan-legislators.yml  # state rosters (dispatch + monthly cron)
    legiscan-bills-votes.yml  # state bills/votes (dispatch + weekly cron)
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
Same secrets as disclosures: `HORIZONS_BASE_URL` + `INGEST_API_KEY`.

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

## California spending (`ca-spending-sync`)

Official site is **Open FI$Cal** ([open.fiscal.ca.gov](https://open.fiscal.ca.gov/)),
run by the Department of FI$Cal under CA GOV §11862. The State Controller’s
Office [points here](https://www.sco.ca.gov/eo_government_data_and_accountability.html)
as the state expenditure checkbook. There is no separate comptroller
“Open Book” for vendor payments.

### Portal structure (verified 2026-08)

| Question | Finding |
|----------|---------|
| Structured data? | **Yes — monthly CSVs on Azure Blob.** No HTML-table scrape. |
| Public API? | **No.** About page: “work toward implementing APIs.” |
| Auth / rate limits | Public anonymous GET. No published rate limit. Files are 100–140 MB/month; the job streams and waits 1.5s between files. Retry/backoff on 429/5xx. |
| Update cadence | **Monthly**, transactions **≥ 60 days old**. Past months are restated. Latest pointer upload observed 2026-08-01 covering FY25 P11 (May 2026). |
| Coverage | 151 departments / 188 business units, ~79% of state expenditures. Not ACFR, not the Governor’s Budget. |
| Vendor vs spending files | **Vendor CSVs only** (have `VENDOR_NAME`). “Spending” CSVs omit the payee and are not ingested. |

Pointer (file index, not a secret):

`https://adwoutputfilesadlsstore.blob.core.windows.net/transparency/MonthlyVendorTransactionPointer/MonthlyVendorTransactionPointer.csv`

Each row is a **modified-accrual AP/GL expenditure line** (cash basis for a
few non-FI$Cal depts). The FAQ is explicit: **contracts are not displayed**;
encumbrances live in the Governor’s Budget; a signed contract is not an
Open FI$Cal expenditure until a payment liability exists. Amounts can be
negative (reversals). Vendor name is `CONFIDENTIAL` for employees and
statutorily masked payees.

California FY **N** = 1 July N … 30 June N+1. `--fiscal-year 2025` means
**FY25 / 2025-26**, matching the portal’s `Vendor_FY25P##.csv` names.
P01 = July, P12 = June.

There is **no per-voucher permalink** on the portal (dashboard + bulk CSV).
`source_url` is a locator on the official download page plus the line’s
natural keys (`document_id`, `account`, `accounting_date`, `program_code`).
It is required and unique; it is not a clickable record page.

### FLAG — do not map `award_type` / `amount_type`

The federal `spending_awards` enums do **not** fit this portal:

| Federal field | Why it does not fit |
|---------------|---------------------|
| `award_type` ∈ contract / grant / loan / direct_payment / insurance | These are journal lines. Account categories mix rent, travel, payroll benefits, interdepartmental consulting, and “Grants and Subventions.” Guessing `grant` from that last label would invent an award type. The portal itself says it does not publish contracts. |
| `amount_type` ∈ obligated / potential | Amount is a **posted expenditure** (modified accrual), not an obligation and not a potential ceiling. |

This job **omits** `award_type` and `amount_type`. It does not send
`obligated`. Horizons must accept those two fields as null/absent when
`jurisdiction=state`. Until that lands, live POSTs will 400 (or 404 —
the awards route is still undeployed). See
`docs/HORIZONS_SPENDING_AWARDS.md`.

### Field mapping

| `spending_awards` | Open FI$Cal column | Notes |
|-------------------|--------------------|-------|
| `award_id` | `CA-{document_id}-{account}-{program_code}-{accounting_date}` | Line-level; one voucher can have many accounts |
| `recipient_name` | `VENDOR_NAME` | `CONFIDENTIAL` → review, not posted |
| `recipient_state` | — | Not in the file. Always `null` |
| `awarding_agency` | `department_name` (fallback `agency_name`) | |
| `award_type` | — | **omitted** (see flag) |
| `amount` | `monetary_amount` | Posted expenditure. Negatives → review |
| `amount_type` | — | **omitted** (see flag) |
| `period_of_performance_start` | `accounting_date` | Transaction date, not a PoP |
| `period_of_performance_end` | — | Always `null` |
| `description` | `program_description` + `account_description` | |
| `source_url` | locator on `monthly_vendor_transaction.html` | Required |
| `fiscal_year` | `fiscal_year_begin` | CA FY begin year |
| `jurisdiction` | — | always `state` |
| `state_code` | — | always `CA` |
| `program` | `program_description` | Extra; Horizons should store or ignore |

### CLI

```bash
# parse only, 5 most-recent FY25 lines
python -m ingest.ca_spending --fiscal-year 2025 --dry-run --max-records 5

# incremental window (accounting_date)
python -m ingest.ca_spending --fiscal-year 2025 --days-back 90 --max-records 20 --dry-run

# same via the orchestrator
python -m ingest.main --source ca_spending --fiscal-year 2025 --dry-run --max-records 5
```

| Flag | Required | Notes |
|------|----------|--------|
| `--fiscal-year` | yes | CA FY **begin** year (2025 = FY25) |
| `--days-back` | no | `accounting_date` window, clipped to the FY. Omit for every published month in that FY |
| `--max-records` | no | Hard cap. Dry-run via `main.py` defaults to 5 |
| `--dry-run` | no | Pointer + stream + map + log; no Horizons POST |

A full FY is ~1.2–1.5 GB across 12 monthly files. Stream; do not download a
year as one file (the portal does not offer one).

### Schedule

Portal cadence is **monthly**, not weekly. Workflow
`.github/workflows/ca-spending.yml` is **manual / dry-run default**. Cron
is commented; suggested `0 14 3 * *` (3rd of month, after the observed
1st-of-month Azure upload). Re-enable only after Horizons accepts state
rows without `award_type` / `amount_type`. Same secrets as USAspending.

### Expected `manual_review` volume

A typical vendor month is on the order of **10⁵ lines** (FY25 P11 is 127 MB).

| Bucket | Disposition | Expected share |
|--------|-------------|----------------|
| Named vendor, amount > 0, parseable date/id | Mapped and POSTed **without** `award_type`/`amount_type` | Majority of vendor-file lines |
| `VENDOR_NAME=CONFIDENTIAL` | Counted as review; **not** queued one-by-one | Common (all employee pay + listed mask rules) |
| `monetary_amount` ≤ 0 | Counted as review; not queued one-by-one | Common (reversals / adjustments) |
| Missing `document_id` / date / amount | Counted as review | Rare |

Per-row `flag_manual_review` is **capped at 25** so a monthly run cannot
flood the queue. The ingestion log records the uncapped review count.
Until Horizons stores state expenditures as expenditures (not awards),
treat the mapped majority as “structurally complete, semantically
unfit for the federal award enums.”

### Sample payload (one vendor line)

```json
{
  "awards": [
    {
      "award_id": "CA-1115.00021375.0.00001.0002-5324450-1460010000-2026-05-29",
      "recipient_name": "CARDINAL INV PROP GODDARD LP",
      "recipient_state": null,
      "awarding_agency": "Department of Cannabis Control",
      "amount": 17152.0,
      "period_of_performance_start": "2026-05-29",
      "period_of_performance_end": null,
      "description": "Cannabis Control - Support — Rent -Bldgs&Grounds(Non State)",
      "source_url": "https://open.fiscal.ca.gov/monthly_vendor_transaction.html?document_id=1115.00021375.0.00001.0002&account=5324450&accounting_date=2026-05-29&program_code=1460010000",
      "fiscal_year": 2025,
      "jurisdiction": "state",
      "state_code": "CA",
      "program": "Cannabis Control - Support"
    }
  ]
}
```

## 50-state spending (`state-spending-sync`)

Same research pass as California, for every USPS state. Catalog:
`ingest/state_portals.json`. Orchestrator: `python -m ingest.state_spending`.

**These are posted expenditures / vendor payments, not federal awards.**
No adapter sets `award_type` or `amount_type`. HTML checkbooks are not
scraped. A state is `unsupported` until a public CSV/SODA/CKAN URL was
confirmed on the official site.

### Live adapters (probed 2026-08-14)

| Code | Portal | Adapter | Grain | Cadence |
|------|--------|---------|-------|---------|
| CA | [Open FI$Cal](https://open.fiscal.ca.gov/) | `open_fiscal` | AP/GL vendor lines | monthly |
| CT | [Open Checkbook](https://opencheckbook.ct.gov/) SODA `ajdm-rvz7` | `socrata` | payment lines | daily |
| DE | [Open Checkbook](https://opencheckbook.delaware.gov/) SODA `5s6n-7hpx` | `socrata` | checks | monthly |
| MA | [CTHRU](https://www.macomptroller.org/cthru/) SODA `pegc-naaa` | `socrata` | Mosaic payments | daily |
| MD | [Vendor Payments](https://vendorpayments.maryland.gov/) SODA `7syw-q4cy` | `socrata` | often FY aggregates | monthly |
| MO | [MAP](https://mapyourtaxes.mo.gov/) SODA `gndj-tfr3` | `socrata` | annual vendor totals (date = FY July 1) | annual |
| NJ | [YourMoney](https://www.nj.gov/transparency/financial-management/expenditures/) SODA `apet-rp2i` | `socrata` | YTD by payee | quarterly |
| OR | [Transparency](https://www.oregon.gov/transparency/pages/expenditures.aspx) SODA `y9g9-xsxs` | `socrata` | cash paid / vendor / FY | annual |
| OK | [OMES CKAN](https://data.ok.gov/dataset/state-of-oklahoma-vendor-payments-fiscal-year-2026) | `ckan` | monthly voucher CSV | monthly |

The other 41 states have an official portal URL in the catalog and are
**skipped** until a stable anonymous file/API is pinned. Typical blockers:
query-only Excel (AL, NY), OpenGov/Tableau SPA (AK, MT, NV), CAPTCHA
(AR), login-gated zips (OH), budget-without-vendor (NC, TX county
rollups, WA fiscal). See each `notes` field. Do not invent Socrata 4x4s.

### CLI

```bash
python -m ingest.state_spending --state CT --fiscal-year 2026 --dry-run --max-records 5
python -m ingest.state_spending --state CA --fiscal-year 2025 --dry-run --max-records 5
python -m ingest.state_spending --all-states --fiscal-year 2026 --dry-run --max-records 3
python -m ingest.main --source state_spending --state OK --fiscal-year 2026 --dry-run
```

`--all-states` walks the catalog: live adapters fetch, `unsupported`
states log and skip. Same flags as USAspending (`--fiscal-year`,
`--days-back`, `--max-records`, `--dry-run`). `jurisdiction=state` and
`state_code` are set on every mapped row. Confidential / non-positive
amounts go to review (capped at 25 flags).

Workflow `.github/workflows/state-spending.yml` is **dispatch / dry-run
default**. Monthly cron is commented until Horizons accepts state rows
without `award_type`/`amount_type` (same hole as CA). Suggested
`30 14 3 * *`.

### Review volume

SODA ledgers are millions of rows. A monthly `--all-states` without
`--max-records` is not a first-run. Start with `--days-back 30
--max-records 500` per live state. MO/OR/NJ/MD are aggregates — lower
volume, but they are **not** comparable to CA voucher lines. Do not sum
them with CA as if they were the same grain.

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

`POST /hcgi/api/ingest/spending/fraud-cases` is live. See
`docs/HORIZONS_FRAUD_CASES.md` for the 50-state / agency contract.

A second job, `python -m ingest.doj_fraud`, pulls DOJ Criminal Division
national Health Care Fraud takedown summaries (2023–2026), the Minnesota
HCF page, and the public DOJ news RSS. USAO district listing pages are
bot-walled and are not scraped.

Each posted row is tagged with `state_code` (venue) and `agency` (victim
federal agency: HHS, SBA, DOL, USDA, VA, DOD, Treasury, …). Some states
will have zero tagged cases until a federal write-up names them — we do
not invent rows.

### What it captures

| Field | Source |
|-------|--------|
| `case_title` | Listing title |
| `agency_program` | Mapped only if title/body names a known federal program |
| `agency` | Victim agency from the program / text (not DOJ-as-prosecutor) |
| `state_code` | USAO district, “District of X”, or a single residence state |
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
python -m ingest.prac --days-back 14 --max-records 10 --post   # write
python -m ingest.doj_fraud --days-back 400                     # dry-run national HCF + RSS
python -m ingest.doj_fraud --days-back 400 --post              # write
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

`$3.9 million` is the figure in the PRAC listing and the DOJ title — not
computed. The same listing’s “over $40,000” PUA case would have
`amount_alleged: null`.

## FJC Article III judges (`fjc-judges-sync`)

Quarterly full-refresh job. **Not an incremental daily sync.** The
directory turns over slowly (new confirmations, senior status, deaths).

Source: the Federal Judicial Center’s
[Biographical Directory of Article III Federal Judges](https://www.fjc.gov/history/judges/biographical-directory-article-iii-federal-judges-export)
nightly flat-file export.

| File | URL |
|------|-----|
| Export page | https://www.fjc.gov/history/judges/biographical-directory-article-iii-federal-judges-export |
| CSV (this job) | https://www.fjc.gov/sites/default/files/history/judges.csv |
| Excel (same data) | https://www.fjc.gov/sites/default/files/history/judges.xlsx |

Checked 2026-08: ~4,074 judges, 201 columns, unique `nid` on every row.
FJC also publishes a relational split (`demographics.csv`,
`federal-judicial-service.csv`, …). We use the flat file so one row is
one judge. Citation FJC asks for: *History of the Federal Judiciary.*
http://www.fjc.gov/.

`POST /hcgi/api/ingest/judicial/judges` is **not deployed** (live 404 on
rollcalltrack.com and the preview origin). See
`docs/HORIZONS_JUDICIAL_JUDGES.md`. Request this route from Horizons
before enabling unattended writes.

### `judge_id` scheme (join key for later judicial jobs)

**`judge_id` = FJC `nid` (Node ID), stored as a string.**

FJC documents `nid` as the unique identifier for each judge in the
biographical database. The same number is the bio-page path:

```
https://www.fjc.gov/node/{nid}
```

Example: Adam Ben Abelson is `nid=13761857` →
https://www.fjc.gov/node/13761857.

**Do not use `jid`.** FJC stopped assigning Judge Identification Numbers
in July 2016. Newer judges have no distinct `jid` (Abelson’s `jid` is
just a copy of `nid`). Later jobs (financial disclosures, confirmation
votes) must join on `nid`.

If FJC ever shipped a row without `nid`, that row goes to
`manual_review` — we do not invent a name+court+date slug.

### Field mapping

Court / president / dates / ABA come from the **highest-numbered
non-empty Article III service slot** (1 = earliest appointment, 6 =
most recent). A judge elevated from district to circuit to SCOTUS
contributes slot 3, not slot 1.

| Field | Source |
|-------|--------|
| `judge_id` | `nid` |
| `full_name` | `First Name` + `Middle Name` + `Last Name` + `Suffix` (blank parts dropped) |
| `court` | `Court Name (n)` on the latest slot |
| `court_type` | mapped from `Court Type (n)` + court name (below) |
| `appointing_president` | `Appointing President (n)` (FJC may write `None (reassignment)`) |
| `nomination_date` | `Nomination Date (n)` → ISO `YYYY-MM-DD`, else null |
| `confirmation_date` | `Confirmation Date (n)` → ISO `YYYY-MM-DD`, else null |
| `aba_rating` | `ABA Rating (n)` as published (`Well Qualified`, `Qualified`, …), else null |
| `status` | derived (below) |
| `source_url` | `https://www.fjc.gov/node/{nid}` |

Live CSV dates are already `YYYY-MM-DD`. FJC’s export notes still say
`mm/dd/yyyy`. The parser accepts both and never invents a date.

`court_type` mapping:

| FJC `Court Type` / name | enum |
|-------------------------|------|
| Supreme Court | `supreme_court` |
| U.S. Court of Appeals; U.S. Circuit Court (historical) | `circuit_court` |
| U.S. District Court | `district_court` |
| U.S. Court of International Trade; U.S. Customs Court | `district_court` |
| U.S. Court of Customs and Patent Appeals | `circuit_court` |
| Court of Claims | `other` |
| anything else | `other` |

`bankruptcy_court` and `magistrate` are in the contract because later
jobs may need them. This directory is **Article III only**. Magistrate /
bankruptcy posts show up as free-text under `Other Federal Judicial
Service` (e.g. Abelson’s 2023–2024 magistrate seat). We do **not**
promote that text to `court` or `court_type`. A sitting district judge
who used to be a magistrate is `district_court`.

`status` mapping, first match wins:

| Condition | enum |
|-----------|------|
| `Death Year` set, or latest `Termination` is `Death` | `deceased` |
| latest `Termination` is `Resignation` or `Impeachment & Conviction` | `resigned` |
| latest `Termination` is `Retirement` | `retired` |
| latest `Senior Status Date` set and no terminal termination | `senior_status` |
| no termination, no senior date, no death year | `active` |
| latest `Termination` is `Recess Appointment-Not Confirmed` or `Abolition of Court` (and no death year) | **manual_review** |

Death always wins: a retired or senior judge who has a death year is
`deceased`. `Appointment to Another Judicial Position` and
`Reassignment` never appear on the last slot in the current export
(they are always followed by a later court).

### CLI

```bash
python -m ingest.fjc --dry-run --full-refresh --max-records 5
python -m ingest.main --source fjc --dry-run --full-refresh --max-records 5
```

| Flag | Required | Notes |
|------|----------|--------|
| `--dry-run` | no | Parse + log; no POST. Prefer this until Horizons exists. |
| `--full-refresh` | no | Accepted and is the only mode. There is no `--days-back` window. |
| `--max-records` | no | Cap rows. `main.py --dry-run` defaults to 5. |

Workflow: `.github/workflows/fjc-judges.yml` — **dispatch is primary**
(`dry_run` defaults to true). Quarterly cron (Jan/Apr/Jul/Oct 15 12:00
UTC) is a backup and also dry-runs until you dispatch with
`dry_run=false` after the Horizons route exists. Same secrets:
`HORIZONS_BASE_URL` + `INGEST_API_KEY`.

### Sample payload

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

Samuel A. Alito Jr. (`nid=1377101`) maps to `Supreme Court of the United
States` / `supreme_court` / `George W. Bush` / `2005-11-10` /
`2006-01-31` — slot 2, not his earlier Third Circuit appointment.

## Judicial scores

Roster is FJC. Events and scores are computed in this repo. Horizons must
implement `docs/HORIZONS_JUDICIAL_SCORES.md` before any `--post`.

Two independent numbers: law-over-politics (`rogue_score` 0–100) and
ideology lean (−100…+100). Appointing party is never a score.

```
python -m ingest.courtlistener_events --dry-run --max-records 5
python -m ingest.scotus_stays --dry-run --max-records 5
python -m ingest.judicial_score --dry-run
python -m ingest.jcs_scores --dry-run
```

Workflows default to dry-run. Flip only after POST
`/hcgi/api/ingest/judicial/events` and `.../scores` stop returning 404.

## AO judicial disclosures (`ao-judicial-disclosures-sync`)

Weekly incremental job. Pulls annual financial disclosure reports and
periodic transaction reports (PTRs) from the Administrative Office of
the U.S. Courts’ public JEFS database, launched November 2022 under the
Courthouse Ethics and Transparency Act.

`POST /hcgi/api/ingest/judicial/disclosures` is **not deployed** (live
404). `GET /hcgi/api/judicial/judges` is also 404. See
`docs/HORIZONS_JUDICIAL_DISCLOSURES.md`. Until the read route exists
the job loads the same FJC export as `fjc-judges-sync` and uses `nid`
as `judge_id`. Do not hardcode a judge list.

This is the most name-matching-fragile job in the module. Ambiguous
JEFS name hits go to **manual_review**. We do not guess.

### Access pattern (live-verified 2026-08)

Official entry:
[uscourts.gov judiciary financial disclosure reports](https://www.uscourts.gov/administration-policies/judiciary-financial-disclosure-reports)
→ **https://pub.jefs.uscourts.gov/**

This is **not** Senate eFD. Differences that matter for maintenance:

| Step | What the site actually does |
|------|-----------------------------|
| Landing page | Registration-only. The search UI is **not** in the unauthenticated HTML. |
| Agreement | Statutory prohibited-use text (5 U.S.C. § 13107) plus 18 U.S.C. § 1001. Not a single checkbox like Senate `prohibition_agreement=1`. |
| Registration | Required **every visit** (“Users must register each time they access this system”). Form fields: first/last name, occupation, address, “Requesting on Behalf of”, certify. POST `/index.php` `action=register` + `_csrf`. |
| reCAPTCHA | Google recaptcha sitekey is on the page (`grecaptcha.execute()`). The site’s own JS calls `register()` even when `verify_recaptcha` returns `success: false`. We do **not** solve captchas. |
| Search | After login, POST `/index.php` `action=search` with `q`, `start`, `sort_by`, `sort_dir`, `facet_string` (JSON). Returns `{ count, filercount, start, rows: [...] }`. Unauthenticated search is `{"document":"You must be logged in"}`. |
| Facets | POST `action=get_facets` works **without** login. Live values: years 2013–2026; report types `Annual`, `Annual/Final`, `Final`, `Initial`, `Periodic Transaction Report`; 201 court strings; positions including District / Circuit / Bankruptcy / Magistrate Judge. |
| Pagination | 10 **filers** per page (`start += 10`). Load-more compares `start + usersRendered < filercount`. |
| Documents | **PDFs only** (JEFS-generated Form AO 10 annual, Form AO 10T PTR). No HTML transaction table. |
| Download | Session cart: POST `action=add\|remove&id={id}`, then GET `/index.php?action=download` (max 100). Unauthenticated download: `You must be registered to download files.` |
| Permalinks | **None.** There is no public `/view/{id}` URL. `source_url` is `https://pub.jefs.uscourts.gov/?filing={id}` — an identifier-bearing locator, not an unauthenticated deep link. A human still has to register to open the file. |
| WAF | F5 cookies. Some GET query strings return HTML `Request Rejected`. Prefer POST. |
| Idle | Client reloads after 30 minutes idle. |
| robots.txt | `User-agent: *` / `Allow: /`. |

Reports on the site are 2022+ that have been cleared for release. 2021
and earlier (and employee reports) are an AO-10A request, not this job.

### Statutory requester identity

5 U.S.C. § 13107 requires a real name, occupation, and address. The
request is a public record. Commercial use other than news/communication
media for dissemination to the general public is prohibited.

The job **refuses to invent** an identity. Set these in `.env` / Actions
secrets, or pass a browser `SESSID` as `JEFS_SESSION`:

`JEFS_FIRST_NAME`, `JEFS_LAST_NAME`, `JEFS_OCCUPATION`, `JEFS_ADDRESS1`,
`JEFS_CITY`, `JEFS_STATE`, `JEFS_ZIP`, optional `JEFS_EMAIL` /
`JEFS_REPRESENTING`.

Without those, a live search exits with a clear error. Offline tests do
not need them.

### Judge matching

Roster, in order:

1. `GET /hcgi/api/judicial/judges` if Horizons has it
2. Else the live FJC `judges.csv` via `ingest.fjc` (`judge_id` = `nid`)

`--judge-id` filters that roster to one nid and searches JEFS by that
judge’s `full_name` (JEFS has no FJC id field).

A JEFS `name_s` matches only when last name is exact and first name is
exact or a ≥3-character prefix. Multiple hits are reduced by
`court_type_s` vs FJC `court`. Anything still 0 or ≥2 → `manual_review`.

### Field mapping

| Field | Source |
|-------|--------|
| `judge_id` | FJC `nid` after a unique name(+court) match |
| `filing_id` | JEFS search row `id` |
| `filing_type` | `Periodic Transaction Report` → `periodic_transaction`; `Annual` / `Annual/Final` / `Initial` / `Final` → `annual` (same AO-10 asset form) |
| `filing_date` | PTR: `ao_certified_dt`; annual: `published_dt` (ISO) |
| `source_url` | `https://pub.jefs.uscourts.gov/?filing={id}` |

PTR transactions (Form AO 10T): `asset_name`, `transaction_type`
(`Buy*` → `purchase`, `Sold*` / `Redeemed` → `sale`, `Exchange` →
`exchange`), `transaction_date`, `amount_range_low` / `_high`.

Annual assets (Form AO 10 Part VII): `asset_name`, `asset_type`
(null unless the PDF states one), `value_range_low` / `_high`.

Brackets are the 5 U.S.C. § 13104(d) ranges / AO-10 letter codes
`J`–`P4`. `P4` / “More than $50,000,000” has `high: null`. We never
store a midpoint. A PTR/annual PDF we cannot parse still keeps the
matched filing shell and goes to `manual_review` for the empty child
rows — live JEFS PDF layout is not public without a registered session,
so the parser is conservative.

### CLI

```bash
# needs JEFS_* identity for a live search
python -m ingest.jefs --dry-run --days-back 14 --max-records 3
python -m ingest.jefs --dry-run --judge-id 1377101 --max-records 5
python -m ingest.main --source jefs --dry-run --days-back 14
```

| Flag | Required | Notes |
|------|----------|--------|
| `--judge-id` | no | FJC nid. Search that judge’s name only. |
| `--days-back` | no | Default 14. Filters published/certified date. Incremental search sorts `f.published_dt` DESC and stops when the page is older. |
| `--max-records` | no | Cap filings. `main.py --dry-run` defaults to 3. |
| `--dry-run` | no | Parse + log; no POST. |

Workflow: `.github/workflows/jefs-disclosures.yml` — **dispatch is
primary** (`dry_run` defaults true). Weekly Monday 12:00 UTC cron is a
backup and also dry-runs until you dispatch `dry_run=false` after both
the Horizons route **and** `JEFS_*` secrets exist.

### Sample payload

```json
{
  "filings": [
    {
      "judge_id": "1377101",
      "filing_id": "ptr-1",
      "filing_type": "periodic_transaction",
      "filing_date": "2026-01-20",
      "source_url": "https://pub.jefs.uscourts.gov/?filing=ptr-1",
      "transactions": [
        {
          "filing_id": "ptr-1",
          "asset_name": "Apple Inc. (AAPL)",
          "transaction_type": "purchase",
          "transaction_date": "2026-01-15",
          "amount_range_low": 15001,
          "amount_range_high": 50000,
          "source_url": "https://pub.jefs.uscourts.gov/?filing=ptr-1"
        }
      ],
      "annual_assets": []
    }
  ]
}
```

`$15,001 - $50,000` is stored as that bracket. The midpoint `$32,500.50`
is never written.

## Senate roll calls (`senate_votes`)

Hostinger's `GET /hcgi/api/sources/votes/senate` live-fetches senate.gov
and returns `count: 0` (checked 2026-10-22: `source: "live"`,
`senateCount: 0`). House roll calls on the same host succeed because
they come from clerk.house.gov. This job fetches the Senate XML from
the scheduled runner (GitHub Actions `ingest.yml` and the Windows daily
task, both `python -m ingest.main --source all`) and POSTs it through
the existing `POST /hcgi/api/ingest/votes` path.

The menu URL is derived from congress and session. Session 1 and
session 2 both work:

`https://www.senate.gov/legislative/LIS/roll_call_lists/vote_menu_{congress}_{session}.xml`

Per-vote detail (member positions) is optional and on by default:

`https://www.senate.gov/legislative/LIS/roll_call_votes/vote{congress}{session}/vote_{congress}_{session}_{00000}.xml`

Requests send a browser `User-Agent` and do not follow redirects. A 3xx
or a non-XML body (HTML block page, JSON) fails the run instead of
being stored as zero votes.

### Payload

Same body as state votes (`{"votes":[...]}`) and the same fields as a
House row already stored on `GET /hcgi/api/votes`. House rows use
`chamber: "House"`. These use `chamber: "Senate"`.

| Field | Source |
|-------|--------|
| `id`, `rc_id` | `s-{congress}-{congress_year}-{roll}` e.g. `s-119-2026-256` |
| `congress` | menu `<congress>` |
| `chamber` | `Senate` (House rows are `House`, not `house`) |
| `session` | menu `<session>` as a string (`"1"` or `"2"`) |
| `roll_number` | `<vote_number>` without padding (`00256` → `256`) |
| `bill_label` | `<issue>` (`PN1129`, `H.R. 9340`, `S. 5`). En-bloc rolls nest the issues under `<en_bloc><matter>` and are stored as `En bloc (N)`. |
| `title`, `description` | `<title>`. Description stays the title. |
| `result` | `<result>` (`Confirmed`, `Agreed to`, `Rejected`, `Passed`, `Well Taken`). En-bloc menus have no direct result; the detail `<vote_result>` is used, or the first `<matter>` when there is no detail. |
| `vote_type`, `question` | `<question>` (`On the Nomination`, …). Both are sent. En-bloc questions come from the detail `<question>` or the first `<matter>`. |
| `date` | `<vote_date>` `30-Sep` plus `<congress_year>` → `2026-09-30`. Detail XML upgrades this to an Eastern Time offset, e.g. `2026-09-30T21:29:00-04:00` (EDT) or `2026-01-07T13:23:00-05:00` (EST). |
| `tally_yea`, `tally_nay` | `<vote_tally><yeas>` / `<nays>` |
| `tally_present`, `tally_not_voting` | `0` from the menu. Detail counts `Present` and `Not Voting`. |
| `jurisdiction` | `federal` |
| `state_code` | `""` (House rows store an empty string, not null) |
| `source_url` | the detail XML URL, one per roll |
| `member_votes[]` | only when detail was fetched. `bioguide_id` from congress-legislators `id.lis` (`S428`). `vote` is `yea` / `nay` / `present` / `not_voting`. Unmapped members are omitted. |

`rc_id` is not `{congress}-{session}-{roll}`. That is the House key
(`119-2-314`) and would collide with a Senate roll of the same number.
`s-119-2026-256` is also what `nominations.py` and the site route
`/congress/senate-vote/{congress}/{year}/{roll}` already expect.

### Assumptions about the Hostinger endpoint

`POST /hcgi/api/ingest/votes` exists (state votes already use it; a GET
without the key is 404 because the route is POST-only). This job
assumes that route upserts federal rows on `id` / `rc_id` / `source_url`
the way disclosure ingest upserts on `source_url`. Re-running the job
sends the same ids, so a second run should update rather than insert a
duplicate. If the route only upserts `jurisdiction=state`, federal
Senate rows will 400 until Horizons accepts `jurisdiction=federal`,
`chamber=Senate`, and an empty `state_code` — the shape House rows
already have in `GET /hcgi/api/votes`.

`GET /hcgi/api/sources/votes/senate` is a live senate.gov fetch
(`ingested.triggered: true`). Writing the votes collection does not, by
itself, prove that source route will stop returning count 0. The
collection read is `GET /hcgi/api/votes`. The public congress list is
`GET /hcgi/api/congress/votes`.

LIS ids are not bioguides. Member positions are joined through the
public congress-legislators roster. If that roster is down, the roll
call is still posted and `member_votes` is omitted.

### CLI

```bash
python -m ingest.main --source senate_votes --dry-run --max-records 5
python -m ingest.main --source senate_votes --congress 119 --session 1 --dry-run
python -m ingest.main --source senate_votes --senate-session 1 --dry-run
python -m ingest.main --source senate_votes --senate-session all --live
```

`--source all` (the daily workflow and `scripts/run_daily_ingest.ps1`)
always uses the current congress and session and ignores `--days-back`,
`--session`, and `--senate-session`, so a disclosure window cannot drop
older rolls in the same session and a manual backfill flag cannot widen
the cron. `--congress` and `--session` apply only with
`--source senate_votes`.

### Backfill session 1 (2025)

The daily job only posts the current session. As of 2026-10-22 that is
session 2 (`s-119-2026-N`, rolls 1–256 from `vote_menu_119_2.xml`).
Session 1 (`vote_menu_119_1.xml`, about 650 rolls, ids `s-119-2025-N`)
is a manual backfill. It uses the same parser: question, en-bloc
results, Eastern Time offsets, and member positions.

Actions → **Disclosure Ingest** → **Run workflow**:

| Input | Values |
|-------|--------|
| `senate_session` | `current` (default; same as the cron), `1`, `2`, or `all` |
| `dry_run` | `true` parses and does not POST. `false` posts. The cron ignores this and posts. |

Run `senate_session=1` with `dry_run=true` first, then the same choice
with `dry_run=false`. `all` posts session 1 and then session 2. A
dispatch that is not `current` runs Senate roll calls only, so it does
not also re-ingest House and Senate disclosures. Leave the inputs at
`current` / `false` to repeat the daily job by hand.

Vote POSTs go out in batches of 25 (one request each) with 3.1s between
batches. That is the same gap as the public list reads. The origin
budget is about 100 requests / 5 minutes (`ratelimit-policy: 100;w=300`).
A refused POST still waits on `Retry-After` inside the existing ingest
backoff. Re-runs send the same `s-119-2025-N` id and the same detail
`source_url`, which is the upsert key.

A menu of 650 rolls is 26 vote batches plus one ingestion log: **27**
origin requests. Session 2's 256 stored rolls are 11 batches. Every
further 25 rolls is one more POST. senate.gov and the legislator roster
are not origin requests.

| | Session 1 (~650 rolls) | `all` (~650 + 256 session-2 rolls) |
|--|-----------|-----------------------------------------------|
| senate.gov menu + detail GETs | 1 + ~650 | 2 + ~906 |
| congress-legislators GET | 1 | 1 |
| Origin POSTs | 26 batches + 1 log = **27** | 26 + 11 batches + 2 logs = **39** |
| Share of 100 requests / 5 min | 27 | 39 |
| Expected duration | about 15–30 min | about 25–40 min |

Detail XML is the long pole: 0.5s between senate.gov fetches, plus
response time. The origin calls themselves are about a minute and a
half of pacing. A dry run does the same senate.gov fetches and zero
origin POSTs. The job timeout is 90 minutes, which also covers a
`Retry-After` wait. Do not overlap this with the 09:00 UTC scheduled
ingest; they share the origin budget. A second live run repeats the
same POSTs and updates rows in place.

## Judicial nominations (`judicial-nominations-sync`)

Weekly **linker**, not a new scrape. Congress.gov already publishes
presidential nominations (PN numbers). Horizons already stores Senate
roll calls at `GET /hcgi/api/congress/votes`. This job connects a
judicial PN to:

1. `judge_id` from `fjc-judges-sync` (name + court, same conservative
   matcher as JEFS)
2. `senate_vote_id` when the confirmation was a recorded roll call

Auth is the existing `CONGRESS_GOV_API_KEY` (same as `congress_enrich`).

`POST /hcgi/api/ingest/judicial/nominations` is **not deployed** (live
404). See `docs/HORIZONS_JUDICIAL_NOMINATIONS.md`.

This is **not** folded into `.github/workflows/ingest.yml`. That
workflow is House/Senate disclosures plus Senate roll calls, and does
not read nominations.

### How a confirmation vote is linked

Horizons vote ids look like `s-119-2026-209` (chamber, congress,
session year, roll). Live check: PN999-5 Antonio M. Pozos, confirmed
2026-07-23, is `s-119-2026-209` (`On the Nomination PN999-5: …`,
result `Nomination Confirmed`). The cloture vote on the same nominee
is `s-119-2026-205` and is **not** used.

`senate_vote_id` is set only when:

- Congress.gov action text has `Record Vote Number: N`, from which we
  build `s-{congress}-{vote_year}-{N}`, or
- a Horizons vote title is `On the Nomination {PN}` and is not cloture

Otherwise it stays **null**. `senate_vote_note` says why, so review can
tell voice vote from a missing roll call:

| `senate_vote_note` | `senate_vote_id` | Meaning |
|--------------------|------------------|---------|
| `linked` | set | Matched a Horizons confirmation vote or constructed id that matches the system scheme |
| `voice_vote` | null | Action text is “Confirmed … by Voice Vote” |
| `unanimous_consent` | null | Action text is unanimous consent |
| `roll_call_constructed` | set | Official Record Vote Number; id built as `s-{congress}-{year}-{roll}` (may be outside Horizons’ 250-vote window) |
| `roll_call_unmatched` | null | Yea-Nay language but no Record Vote Number |
| `no_recorded_vote` | null | Terminal action, no vote language |

Pending nominations (referred / reported, no confirm/reject/withdraw/return)
are **skipped**, not reviewed.

US Attorneys, cabinet, ambassadors are filtered out. Judicial means
district / circuit / SCOTUS / CIT / Claims Court language in the
description. “Judge Advocate” is excluded.

### Field mapping

| Field | Source |
|-------|--------|
| `nomination_id` | Congress.gov `citation` (`PN999-5`) |
| `judge_id` | FJC `nid` after unique name+court match |
| `nominating_president` | FJC `appointing_president` on the matched judge (Congress.gov list/detail has no president name). Null if unmatched. |
| `vote_date` | Terminal action `actionDate` |
| `vote_result` | `Confirmed by the Senate` → `confirmed`; `Rejected by the Senate` → `rejected`; withdrawn / returned to President → those enums |
| `senate_vote_id` | Horizons vote id or constructed `s-{congress}-{year}-{roll}` |
| `source_url` | `https://www.congress.gov/nomination/{nth}-congress/{number}` |

Ambiguous name matches (two John Smiths, court does not separate them)
go to **manual_review**.

### CLI

```bash
python -m ingest.nominations --congress 119 --days-back 14 --dry-run
python -m ingest.nominations --congress 118 --dry-run --max-records 20
python -m ingest.main --source nominations --congress 119 --dry-run
```

| Flag | Required | Notes |
|------|----------|--------|
| `--congress` | no | Defaults to the current Congress (119 as of 2026-08). |
| `--days-back` | no | Default 14. Passed as `fromDateTime` on the list endpoint. |
| `--dry-run` | no | Parse + log; no POST. |
| `--max-records` | no | Cap. `main.py --dry-run` defaults to 10. |

Needs `CONGRESS_GOV_API_KEY` even for dry-run (live API). Workflow:
`.github/workflows/judicial-nominations.yml` — dispatch primary
(`dry_run` default true). Weekly Monday 13:00 UTC backup, also
dry-run until you dispatch `dry_run=false` after Horizons exists.

### Sample payload

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

A voice-vote confirmation of the same shape would have
`senate_vote_id: null` and `senate_vote_note: "voice_vote"`.

## LegiScan legislators (`legiscan-legislators-sync`)

Monthly roster job. Pulls current-session legislators for the 50 states
from the LegiScan public API (`getSessionList` + `getSessionPeople`)
and POSTs them to the **existing** `POST /hcgi/api/ingest/members`.

No `state_members` table. Each row is a `members` record with
`jurisdiction=state` and a 2-letter `state_code`.

Congress is **not** pulled from LegiScan. Federal members stay on the
existing Congress.gov / Horizons member-sync path.

`POST /hcgi/api/ingest/members` is **live** (401 without a key, not
404). It must accept `jurisdiction`, `state_code`, and
`chamber=unicameral`. If those 400 as unexpected fields, that is a
schema addition on `members` — not a new route. See
`docs/HORIZONS_LEGISCAN_MEMBERS.md`.

Needs `LEGISCAN_API_KEY` (free at https://legiscan.com/legiscan).
Two queries per state (~100 for all 50). Do not call `getPerson` per
legislator.

### Field mapping

| Field | Source |
|-------|--------|
| `bioguide_id` | `LS{people_id}` — cannot collide with a congressional bioguide |
| `name` | LegiScan `name` |
| `party` | `D` / `R` / `I` (same letters as federal) plus `N` = Nonpartisan. Third-party / blank → **manual_review** |
| `state` | 2-letter USPS |
| `state_code` | same 2-letter code |
| `jurisdiction` | always `state` |
| `chamber` | `Rep`/`Assembly`/`Delegate` → `house`; `Sen` → `senate`; **Nebraska always `unicameral`** |
| `district` | integer parsed from `HD-111` / `SD-3` / `12`. Non-numeric labels → review |
| `role` | `Representative, District N` / `Senator, District N` |
| `source_url` | `https://legiscan.com/{ST}/people/{people_id}` (stable people_id path) |

Existing `members.district` is a number. We do not invent a district
from “A” or an at-large label.

### CLI

```bash
python -m ingest.legiscan --state DE --dry-run
python -m ingest.legiscan --state NE --dry-run
python -m ingest.legiscan --all-states --dry-run
python -m ingest.main --source legiscan --state TX --dry-run
```

| Flag | Required | Notes |
|------|----------|--------|
| `--state` | one of | Single USPS code. Use this first. |
| `--all-states` | one of | All 50. Not DC, not Congress. |
| `--dry-run` | no | Parse + log; no POST. |

Workflow: `.github/workflows/legiscan-legislators.yml` — dispatch
stays dry-run unless `dry_run=false`. **Monthly cron (1st, 12:00 UTC)
POSTs all 50 states** to `/hcgi/api/ingest/members` (route exists).
Needs repo secret `LEGISCAN_API_KEY`.

### Test-run notes (data quality)

Live dry-run 2026-08-14 against current sessions (DE, NE, WV, TX, CA):

| State | Session | Found | Mapped | Review | Skip (committees) |
|-------|---------|------:|-------:|-------:|------------------:|
| DE | 2025-2026 Regular | 65 | 65 | 0 | 0 |
| WV | 2026 Regular | 134 | 134 | 0 | 0 |
| TX | 2025 Regular | 181 | 181 | 0 | 0 |
| CA | 2025-2026 Regular | 160 | 121 | 0* | 39 |
| NE | 2025-2026 Regular | 64 | 50 | 0 | 14 |

\*CA’s 39 leftover rows are `committee_sponsor=1` (Budget and Fiscal
Review, Agriculture, …). They are skipped, not posted as members.

**Nebraska is not incomplete — it is nonpartisan.** The Unicameral is
officially nonpartisan; LegiScan (and the FEC) code that as `N`. That is
not Independent and not missing data. We store `party=N` and
`chamber=unicameral`. Do not map `N` → `I`. Horizons must accept `N`
on `members.party` (federal rows stay `D`/`R`/`I`).

The other 14 NE rows are committees / Executive Board
(`committee_sponsor=1`) and are skipped.

DE/WV/TX matched 100%. TX 181 = 150 House + 31 Senate. WV 134 = 100 + 34.
CA 121 people vs the usual 120 seats — one extra person in the session
list; not guessed away.

### Sample payload

```json
{
  "members": [
    {
      "bioguide_id": "LS2240",
      "name": "Wendell Gilliard",
      "party": "D",
      "state": "SC",
      "chamber": "house",
      "district": 111,
      "role": "Representative, District 111",
      "jurisdiction": "state",
      "state_code": "SC",
      "source_url": "https://legiscan.com/SC/people/2240"
    }
  ]
}
```

## LegiScan bills + votes (`legiscan-bills-votes-sync`)

Weekly incremental job. For each state: `getSessionList` → current (or
`--session`) session → `getMasterList` → `getBill` only for bills whose
`last_action_date` is inside `--days-back` → `getRollCall` for each
vote stub on those bills.

Same collections as federal. `jurisdiction=state` + `state_code`. No
`state_bills` / `state_votes` tables.

Legislator lines on a roll call join with `bioguide_id = LS{people_id}`.
If `people_id` is missing, that line is dropped — no name matching.

### Horizons routes

| Route | Live 2026-08-14 |
|-------|-----------------|
| `POST /hcgi/api/ingest/bills` | **404** — request this (existing collection, new or enabled ingest) |
| `POST /hcgi/api/ingest/votes` | exists (401 without key) |
| `POST /hcgi/api/ingest/state-coverage` | **404** — new |

`bills` currently uniques `(congress, bill_type, bill_number)`. State
rows use `congress` = session `year_start`. **TX HB1 and CA HB1 will
collide** unless the unique key includes `state_code` or upserts on
`id=LS{bill_id}`. See `docs/HORIZONS_LEGISCAN_BILLS.md`.

### Field mapping — bills

| Field | Source |
|-------|--------|
| `id` | `LS{bill_id}` |
| `congress` | session `year_start` (not a US Congress number) |
| `bill_type` / `bill_number` | LegiScan `bill_type` / `HB1` |
| `title` / `description` | as published |
| `legislation_status` | 1–6 → Introduced / Engrossed / Enrolled / Passed / Vetoed / Failed |
| `became_law` | true only if latest action text contains “signed” |
| `latest_action_*` | last history row / masterlist last_action |
| `jurisdiction` / `state_code` | `state` / USPS |
| `source_url` | LegiScan bill page (`https://legiscan.com/DE/bill/HB1/2025`) |

### Field mapping — votes

| Field | Source |
|-------|--------|
| `id` | `{st}-{year}-{roll_call_id}` e.g. `de-2025-1574346` |
| `rc_id` | `LSRC{roll_call_id}` |
| `chamber` | H→house, S→senate, NE→unicameral |
| `result` | passed=1 → Passed else Failed |
| `tally_*` | yea / nay / nv+absent as not_voting |
| `source_url` | `https://legiscan.com/DE/rollcall/HB1/id/1574346` |
| `member_votes[].bioguide_id` | `LS{people_id}` |
| `member_votes[].vote` | Yea→yea, Nay→nay, Absent/NV→not_voting |

### Coverage tracker (`state_coverage`)

Internal glance table, not a public page (can feed one later).

| Field | Meaning |
|-------|---------|
| `state_code` | USPS, unique |
| `legislators_last_synced` | last successful roster POST |
| `bills_votes_last_synced` | last successful bills/votes POST |
| `completeness_note` | free text (full / window-empty / no roll calls / error) |
| `last_error` | nullable; exception text, never silent under-report |

A legislators run and a bills run **merge** — they must not wipe the
other timestamp. Completeness notes are written even on dry-run (logs
only).

### CLI

```bash
python -m ingest.legiscan_bills --state DE --days-back 14 --dry-run
python -m ingest.legiscan_bills --state DE --session 2163 --days-back 30 --dry-run
python -m ingest.main --source legiscan_bills --state DE --dry-run
```

| Flag | Required | Notes |
|------|----------|--------|
| `--state` / `--all-states` | one of | |
| `--session` | no | LegiScan `session_id`. Default = current regular session. |
| `--days-back` | no | Default 14. Filters `last_action_date`. |
| `--dry-run` | no | Parse + log. |
| `--max-records` | no | Cap bills after the date filter. `main.py --dry-run` defaults to 5. |

Workflow: `.github/workflows/legiscan-bills-votes.yml` — dispatch
stays dry-run unless `dry_run=false`. **Weekly Monday 14:00 UTC POSTs
all 50 states.** `/votes` exists; `/bills` and `/state-coverage` 404
are fail-soft so votes still land. Needs `LEGISCAN_API_KEY`. Horizons
must extend bills uniqueness with `state_code` before bills can persist.

### Test-run notes (DE, 14-day window, 2026-08-14)

DE masterlist = **1296** bills in 2025–26 Regular (`session_id` 2163).
Last 14 days = **4 bills, 8 roll calls, 0 review**. Coverage note:
“full session data available; incremental window has bills and roll
calls.” Last 7 days was 0 (latest actions sat 8–10 days back).

HB462 latest action is “Enact w/o Sign by Governor” — that is treated
as `became_law` (law without a signature), not guessed from status
alone.

Do not `--all-states` until `/ingest/bills` and `/state-coverage` exist
and the bills unique key includes `state_code`.

Do not `--all-states` until `/ingest/bills` and `/state-coverage` exist
and the bills unique key includes `state_code`.
