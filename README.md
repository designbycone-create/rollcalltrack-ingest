# rollcalltrack-ingest

External scheduled job that pulls U.S. House and Senate financial disclosure
filings (free/official sources), parses them, and writes into the
rollcalltrack.com database. Runs independently of Hostinger Horizons —
Horizons only ever reads from this database; it never scrapes anything itself.

## Why this is a separate project

Horizons builds the site (schema, API layer, UI). This repo is the thing that
keeps that schema's `disclosure_filings`, `transactions`, and `annual_assets`
tables full of current, accurate data. It's plain Python, runs on a schedule,
and has no dependency on Horizons other than the database connection string.

## Setup

Horizons stores data in **PocketBase** (not Postgres). The ingest job
authenticates as a PocketBase superuser and writes to the live collections
`disclosure_filings`, `transactions`, `ingestion_log`, etc.

1. `pip install -r requirements.txt`
2. Copy `.env.example` to `.env` and set:
   - `DATABASE_URL` — PocketBase base URL, e.g.
     `https://<app-id>.app-preview.com/hcgi/platform`
   - `POCKETBASE_ADMIN_EMAIL` / `POCKETBASE_ADMIN_PASSWORD` — superuser
   - `CONGRESS_GOV_API_KEY` — https://api.congress.gov/sign-up/
3. Test parse-only (no DB): `python -m ingest.main --source house --dry-run`
4. Test a real write once secrets are set (members table must be populated
   for name matching to succeed).

## Running on a schedule (GitHub Actions — free)

`.github/workflows/ingest.yml` runs this daily. Secrets required:

| Secret | Example |
|--------|---------|
| `DATABASE_URL` | `https://…app-preview.com/hcgi/platform` |
| `POCKETBASE_ADMIN_EMAIL` | superuser email |
| `POCKETBASE_ADMIN_PASSWORD` | superuser password |
| `CONGRESS_GOV_API_KEY` | Congress.gov API key |

Trigger manually from the Actions tab before relying on the daily cron.

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

`db.find_member_id` normalizes filer names and tries several strategies before
giving up (exact variants → alnum-normalized → first/last columns →
last-name + first-name prefix → state/district disambiguation). House passes
`StateDst` (e.g. `AL04`); Senate passes the office string
(`Capito, Shelley Moore (Senator)`). Ambiguous multi-matches return `None`
rather than attaching a filing to the wrong member.

```bash
# offline unit tests for name variants
python tests/test_names.py   # run from repo root with PYTHONPATH=.
```

## Folder structure

```
rollcalltrack-ingest/
  ingest/
    __init__.py
    config.py          # env var loading
    db.py               # DB connection + upsert helpers
    house.py             # House bulk feed download + PDF parsing
    senate.py            # Senate eFD scrape + parsing
    congress_enrich.py   # Congress.gov member enrichment
    main.py              # orchestrator / CLI entry point
  .github/workflows/
    ingest.yml            # scheduled GitHub Actions workflow
  requirements.txt
  .env.example
```
