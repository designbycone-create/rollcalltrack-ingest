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
    congress_enrich.py   # Congress.gov member enrichment (legacy path)
    main.py              # orchestrator / CLI entry point
  .github/workflows/
    ingest.yml           # daily cron + workflow_dispatch
  requirements.txt
  .env.example
```
