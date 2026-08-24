# FOIA / public records: hub + jurisdiction pages

Date: 2026-08-23  
Repo: `designbycone-create/rollcalltrack-ingest`  
Site: https://rollcalltrack.com/foia  
Status: design approved in conversation (hub-and-detail, federal + 50 states, guided form, official PDFs when they exist)

## Decisions (locked)

- **One product, many URLs.** Nav item **FOIA / Public records**. Same chrome and same guided form everywhere.
- **Federal and state on the hub.** `/foia` is the pillar: toggle Federal | State, search, explainer, form, letter, downloads.
- **SEO pages are real paths**, not query-only:
  - `/foia` hub
  - `/foia/federal/{slug}` one federal component
  - `/foia/{state}` one state hub (`al`…`wy`)
  - `/foia/{state}/{agency}` one statewide office we catalog
- `/foia?state=al` and `/foia?agency=fbi` **canonical** to the path URLs (redirect 301).
- **We do not file, store, or track the user’s request.** Submit is a link to FOIA.gov or the state’s official portal.
- **Both templates:** on-page letter + generated fillable PDF + DOCX. Official government PDF is a **second** download when we have a verified URL.
- **Directory depth:** FOIA.gov federal components + 50 state hubs + confirmed **statewide** offices (governor, legislature, SOS, AG, and courts only if the statute covers them). No county/city pages. Each state hub has a short “how to ask a local body under this statute” block.
- **No invented contacts.** Email, portal, form URL, deadline days, and fee rules only from an official page or the FOIA.gov API. If unknown, omit and say “see the official page.”

## What FOIA is (do not blur this on the site)

- **Federal:** Freedom of Information Act, 5 U.S.C. § 552. Executive agencies. Not Congress, not federal courts, not the President’s purely personal papers. Submit via [FOIA.gov](https://www.foia.gov) or the agency FOIA office.
- **State:** Not FOIA. Each state has its own public-records / open-records / sunshine statute, deadlines, and fees. Name the **actual statute** on that state’s page.
- A written request that reasonably describes the records is enough. No federal form is required. Some states publish an optional form; we link it when it exists.

## GitHub vs Horizons

| Work | Owner |
|---|---|
| `public_records_jurisdictions` catalog JSON (50 states + federal row) | This repo. Hand-maintained, sourced from AG / SOS / FOIA.gov pages. Tests refuse a row with no `source_url`. |
| Federal component ingest from `https://api.foia.gov/api/agency_components` | This repo. Needs `FOIA_GOV_API_KEY`. |
| Official form URL harvest (link, not a blind PDF scrape) | Catalog field `official_form_url`. Download optional later. |
| PocketBase collections + ingest + public GET | Horizons |
| `/foia` UI, form, letter, PDF/DOCX, detail pages, nav, sitemap | Horizons |

Workers stay `--dry-run` until ingest routes return 401/200, not 404.

## Collections

### `public_records_jurisdictions`

One row for `federal` and one per USPS state.

| Field | Rules |
|---|---|
| `jurisdiction` | `federal` or `state` |
| `state_code` | 2-letter, required if state, null if federal |
| `name` | “Federal FOIA” or “Alabama Open Records Act” (common name) |
| `statute_citation` | e.g. `5 U.S.C. § 552` / `Ala. Code § 36-12-40`. Official source only |
| `response_days` | integer or null. Business vs calendar stored in `response_days_unit`: `business` \| `calendar` \| null |
| `fee_summary` | short official-sourced text or empty |
| `portal_url` | FOIA.gov request URL or state portal. Required |
| `guidance_url` | AG / DOJ OIP guide |
| `official_form_url` | nullable. State/agency PDF or web form |
| `official_form_title` | nullable |
| `local_how_to` | state only: how a county/city request works under this statute. No fake local emails |
| `exemptions_note` | short; “see statute” if we do not have a sourced summary |
| `source_url` | required, unique upsert. The official page we took the row from |

### `public_records_offices`

Federal components and statewide offices.

| Field | Rules |
|---|---|
| `slug` | unique, URL-safe. Federal: FOIA.gov abbreviation+component. State: `{state}-{office}` e.g. `al-sos` |
| `jurisdiction` | `federal` \| `state` |
| `state_code` | null if federal |
| `parent_agency` | department name |
| `office_name` | component or office |
| `abbreviation` | nullable |
| `foia_email` | nullable; official only |
| `request_url` | portal or web form |
| `website` | office site |
| `reading_room_url` | nullable |
| `submission_methods` | text: portal / email / mail |
| `mail_address` | nullable |
| `official_form_url` | nullable (office-level form beats jurisdiction form) |
| `source_url` | required, unique upsert |
| `foia_gov_id` | federal only, FOIA.gov component UUID |

Do not create `state_foia_*` parallel tables.

## Public GET

- `GET /hcgi/api/public-records/jurisdictions` — all 51. Filter `state`.
- `GET /hcgi/api/public-records/offices` — filters: `jurisdiction`, `state`, `q`.
- `GET /hcgi/api/public-records/offices/{slug}`

No key. List/view public. Create/update API-only (`X-Ingest-Key`).

## Ingest

- `POST /hcgi/api/ingest/public-records/jurisdictions` `{ "jurisdictions": [ ... ] }`
- `POST /hcgi/api/ingest/public-records/offices` `{ "offices": [ ... ] }`
- Upsert on `source_url` (and `slug` for offices). Ignore unknown extra keys. 400 on bad `state_code` / missing `source_url`.

### Federal job

`python -m ingest.foia_federal`  
`GET https://api.foia.gov/api/agency_components?include=agency`  
Map title, abbreviation, request form, website, email, reading room. `jurisdiction=federal`.  
Secret: `FOIA_GOV_API_KEY`. Schedule: monthly + dispatch. Default dispatch dry-run.

### State catalog job

`python -m ingest.public_records_states`  
Reads `ingest/data/state_public_records.json` (51 rows: federal + 50).  
CI test: 50 unique state codes, every row has `source_url` on an official hostname (`.gov` / `.mil` / foia.gov), no empty `portal_url`.  
Do not invent `response_days` or emails in JSON. Null is valid.

## Guided form (client-side)

Fields: name, address, email, phone, agency (from directory), records description, date range, fee category (`media` / `education` / `commercial` / `other`), fee cap, fee-waiver checkbox + one-sentence public-interest reason, date.

Outputs on the same page:

1. Letter preview (copy button)
2. Download PDF
3. Download DOCX
4. If `official_form_url`: “Official {state/agency} form”
5. Primary CTA: **Submit on FOIA.gov** or **Open {state} portal** (new tab). We never POST the letter to an agency.

PDF/DOCX generated in the browser from the letter text. No server store of requester PII.

Letter must include: FOIA or named state statute, reasonably specific records, requester name and contact, fee category, fee cap or waiver request.

## Page copy (hub)

1. Search reading rooms / already-public records first (link judicial, spending, votes on this site where relevant).
2. Federal vs state: which law, 20 business days vs that state’s clock.
3. How to describe records (dates, offices, document types). FOIA does not require the agency to create new records or answer questions.
4. Fees and waivers.
5. Nine federal exemptions in plain English; state pages say exemptions differ and point at the statute.
6. Administrative appeal, then court. We are not a law firm. Disclaimer in footer of `/foia`.

## SEO

- Unique `<title>` and H1 per path.
- Prerender `/foia`, `/foia/federal/*`, `/foia/{state}`, `/foia/{state}/{agency}`.
- Sitemap those paths.
- JSON-LD `HowTo` on the hub; `GovernmentService` optional on state hubs.
- Canonical path URLs.

## Out of scope (this pass)

- User accounts, saved drafts, request tracking
- Filing on the user’s behalf
- County/city directories
- Congress / federal court “FOIA” (they are not FOIA)
- Extra LegiScan keys or other ingest

## Prompt order for Horizons

1. Collections + ingest + public GET  
2. Hub `/foia` (explainer + form + letter + PDF/DOCX + federal/state toggle)  
3. Federal office pages + search wired to GET  
4. State hubs + statewide offices + nav, sitemap, canonicals, prerender  

Paste from `docs/HORIZONS_FOIA.md`.
