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
    congress_enrich.py   # Congress.gov member enrichment (legacy path)
    main.py              # orchestrator / CLI entry point
  .github/workflows/
    ingest.yml           # daily disclosure cron + workflow_dispatch
    usaspending.yml      # daily USAspending cron + workflow_dispatch
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
