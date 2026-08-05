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

1. `pip install -r requirements.txt`
2. Get your database connection string from Horizons (Settings → Database,
   or ask it directly: "what's the connection string for the database you
   provisioned?"). Set it as `DATABASE_URL`.
3. Get a free Congress.gov API key: https://api.congress.gov/sign-up/
   Set it as `CONGRESS_GOV_API_KEY`.
4. Copy `.env.example` to `.env` and fill in both values for local testing.
5. Test locally: `python -m ingest.main --source house --dry-run`
   (`--dry-run` parses and logs but doesn't write to the DB — use this first)

## Running on a schedule (GitHub Actions — free)

`.github/workflows/ingest.yml` runs this daily. To activate:

1. Push this repo to GitHub (private repo is fine — Actions works on private
   repos too, with generous free minutes for a job this small and infrequent).
2. In the repo's Settings → Secrets and variables → Actions, add:
   - `DATABASE_URL`
   - `CONGRESS_GOV_API_KEY`
3. The workflow runs automatically on the schedule defined in the yml file.
   You can also trigger it manually from the Actions tab any time.

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
