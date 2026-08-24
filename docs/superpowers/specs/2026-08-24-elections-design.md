# Elections / midterms: hub + race pages

Date: 2026-08-24  
Repo: `designbycone-create/rollcalltrack-ingest`  
Site: https://rollcalltrack.com/elections  
Status: design approved in conversation (hub-and-detail, federal + 50 states + every legislative district, FEC money on federal races, sourced voter-rule summaries)

## Decisions (locked)

- **One product, many URLs.** Nav item **Elections**. Same chrome everywhere.
- **Hub includes this cycle and future cycles.** `/elections` is the pillar: toggle Federal | State, calendar, ballot, results when an official source publishes them. 2026 is the first cycle (midterms; federal general Tuesday, November 3, 2026). 2028 uses the same shape later.
- **SEO pages are real paths**, not query-only:
  - `/elections` hub
  - `/elections/{cycle}` e.g. `/elections/2026`
  - `/elections/{cycle}/{state}` e.g. `/elections/2026/tx`
  - `/elections/{cycle}/{state}/{race}` e.g. `/elections/2026/tx/us-senate`, `us-house-21`, `governor`, `sd-3`, `hd-12`
- `/elections?state=tx` and `/elections?year=2026` **canonical** to the path URLs (redirect 301).
- **Full ballot map.** Federal House/Senate/specials + statewide executives + every state house/senate (or unicameral) district URL.
- **State legislative district pages do not invent challengers.** Incumbent from members we already host, `is_term_up` only when we know it, required link to that state’s official candidate list / sample ballot. Extra names only with a `source_url` on an official hostname.
- **Federal race pages list FEC-filed candidates** plus cycle totals: receipts, disbursements, cash on hand. Not the donor database (later FEC product).
- **Voter-admin:** sourced registration deadline, voter ID note, absentee note per state, plus vote.gov / SOS links. We do not host a registration form or a polling-place lookup. We do not submit anything to an elections office.
- **No invented dates, dollars, names, or rules.** Official `.gov` / `.mil` / `.us` page or FEC OpenFEC, or omit and say “see the official page.”

## What this page is (do not blur this on the site)

- **Federal:** House, Senate, and specials under federal campaign-finance law. Candidate lists and money from the FEC. The federal general date is set by statute. Ballot access is still state law.
- **State:** Each secretary of state (or equivalent) runs that state’s elections. Statewide offices and legislative districts are not FEC races. Name the official SOS portal on that state’s page.
- We are a transparency directory, not an elections office and not a campaign.

## GitHub vs Horizons

| Work | Owner |
|---|---|
| `elections_jurisdictions` catalog JSON (federal + 50 states: SOS, dates, voter rules) | This repo. Hand-maintained. Tests refuse a row with no official `source_url`. |
| OpenFEC candidate + totals ingest | This repo. `FEC_API_KEY` (api.data.gov; same key type as FOIA). |
| State legislative race rows from current members | This repo. No invented challengers. `is_term_up` null unless the catalog says that chamber is up. |
| PocketBase collections + ingest + public GET | Horizons |
| `/elections` UI, race pages, nav, sitemap, prerender | Horizons |
| Certified results ingest | Later. Result fields exist on races and stay null. |

Workers stay `--dry-run` until ingest routes return 401/200, not 404.

## Collections

Do not create `state_elections_*` parallel tables. Cycle is a field, not its own collection.

### `elections_jurisdictions`

One row for `federal` and one per USPS state.

| Field | Rules |
|---|---|
| `jurisdiction` | `federal` or `state` |
| `state_code` | 2-letter, required if state, null if federal |
| `name` | “Federal elections” or “Texas elections” |
| `sos_portal_url` | required. FEC.gov for federal; SOS (or equivalent) for state |
| `vote_gov_url` | nullable. Usually `https://www.vote.gov/` |
| `registration_url` | nullable. Official registration page |
| `registration_deadline` | date nullable. Do not invent |
| `registration_deadline_note` | short sourced text or empty |
| `voter_id_note` | short sourced text or empty |
| `absentee_note` | short sourced text or empty |
| `primary_date` | date nullable |
| `runoff_date` | date nullable |
| `general_date` | date nullable. 2026 federal general is `2026-11-03` when sourced from FEC |
| `source_url` | required, unique upsert |

Indexes: jurisdiction, state_code, source_url.

### `elections_races`

One row per seat per cycle. A special election for the same seat is a **second** row (`is_special` true, distinct slug).

| Field | Rules |
|---|---|
| `slug` | unique, URL-safe, min length 2. See slug table |
| `cycle` | integer, even year the general is named for (`2026`) |
| `jurisdiction` | `federal` or `state` |
| `state_code` | 2-letter required except president (`null`) |
| `office` | enum below |
| `district` | text nullable. House/state district number as unpadded digits (`21`, `3`). Empty for statewide / Senate / president |
| `office_name` | display name, e.g. “U.S. House Texas District 21” |
| `is_special` | bool, default false |
| `primary_date` | date nullable |
| `runoff_date` | date nullable |
| `general_date` | date nullable |
| `incumbent_name` | nullable |
| `incumbent_bioguide_id` | nullable. Federal bioguide or state LegiScan `LS*` id already stored as `bioguide_id` on members |
| `is_term_up` | bool nullable. `true` only when we know. All U.S. House seats are up in 2026. U.S. Senate only Class 2. State senate often staggered — null if unknown |
| `sos_ballot_url` | nullable. Official candidate list / sample ballot |
| `certified_winner` | nullable text. Empty until official result ingest |
| `result_source_url` | nullable |
| `source_url` | required, unique upsert |

`office` enum: `us_president`, `us_senate`, `us_house`, `governor`, `lieutenant_governor`, `attorney_general`, `secretary_of_state`, `treasurer`, `auditor`, `other_statewide`, `state_senate`, `state_house`, `unicameral`.

Indexes: slug, cycle, jurisdiction, state_code, office, source_url.

#### Slug table

`{cycle}-{state}-{office-key}` lowercase.

| Race | Slug |
|---|---|
| U.S. Senate Texas | `2026-tx-us-senate` |
| U.S. House TX-21 | `2026-tx-us-house-21` |
| Special for that House seat | `2026-tx-us-house-21-special` |
| President | `2028-us-president` (no state) |
| Governor | `2026-tx-governor` |
| Lt. governor | `2026-tx-lt-governor` |
| Attorney general | `2026-tx-ag` |
| Secretary of state | `2026-tx-sos` |
| Treasurer | `2026-tx-treasurer` |
| Auditor | `2026-tx-auditor` |
| Other statewide | `2026-tx-statewide-{short-slug}` |
| State senate district 3 | `2026-tx-sd-3` |
| State house district 12 | `2026-tx-hd-12` |
| Nebraska unicameral district 5 | `2026-ne-leg-5` |

Site path `/elections/2026/tx/us-house-21` loads slug `2026-tx-us-house-21`.

### `elections_candidates`

Federal FEC candidates for v1. State legislative challengers are **not** rows.

| Field | Rules |
|---|---|
| `race_slug` | required, matches `elections_races.slug` |
| `cycle` | integer |
| `state_code` | 2-letter nullable (null for president) |
| `fec_candidate_id` | required for federal, e.g. `H8TX21000` |
| `name` | required |
| `party` | enum `D`, `R`, `I`, `N`, `O` |
| `party_raw` | FEC party string as published |
| `is_incumbent` | bool |
| `bioguide_id` | nullable, when we can match a member |
| `receipts` | number nullable (USD, OpenFEC total) |
| `disbursements` | number nullable |
| `cash_on_hand` | number nullable |
| `coverage_end_date` | date nullable |
| `source_url` | required, unique upsert. FEC candidate page |

Indexes: race_slug, cycle, state_code, fec_candidate_id, source_url.

## Public GET

- `GET /hcgi/api/elections/jurisdictions` — all 51. Filter `state`, `jurisdiction`.
- `GET /hcgi/api/elections/races` — filters: `cycle`, `jurisdiction`, `state`, `office`, `q`, `page`, `limit`. Return `{ ok, page, limit, total, items }`.
- `GET /hcgi/api/elections/races/{slug}` — `{ ok, item }` or 404.
- `GET /hcgi/api/elections/candidates` — filters: `race` (slug), `cycle`, `state`, `page`, `limit`. Return `{ ok, page, limit, total, items }`.

No key. List/view public. Create/update API-only (`X-Ingest-Key`).

## Ingest

- `POST /hcgi/api/ingest/elections/jurisdictions` `{ "jurisdictions": [ ... ] }`
- `POST /hcgi/api/ingest/elections/races` `{ "races": [ ... ] }`
- `POST /hcgi/api/ingest/elections/candidates` `{ "candidates": [ ... ] }`

Upsert jurisdictions and candidates on `source_url`. Upsert races on `slug` (also unique `source_url`). 400 on missing `source_url`, invalid `state_code`, `jurisdiction=state` without `state_code`, invalid `office`, or race missing `slug`. Ignore unknown extra keys. Text over 2000 chars: ingest clips before POST (PocketBase 2048).

### Jobs

`python -m ingest.elections_catalog`  
Reads `ingest/data/elections_jurisdictions.json` (51 rows). CI: 50 unique state codes, every `source_url` on `.gov` / `.mil` / `.us`, no empty `sos_portal_url`. Null dates and notes are valid.

`python -m ingest.fec_elections`  
OpenFEC `candidates` + totals for the cycle. Map to races (create House/Senate/special race rows as needed) and candidate rows. Secret: `FEC_API_KEY`. Default dispatch dry-run.

`python -m ingest.elections_state_races`  
From hosted state members: one race row per district. `sos_ballot_url` from the jurisdiction catalog. No candidate rows. `is_term_up` only from catalog rules, else null.

## Guided UI (client-side)

No PII POST. No donations. No registration form.

Federal race CTA: Open FEC.gov (candidate or elections search).  
State hub CTA: Open SOS portal and vote.gov.  
District CTA: Official ballot / candidate list.

Empty results copy: “Certified results appear here after the official source publishes them.” No unofficial live ticker.

## Page copy (hub)

1. This cycle (2026 midterms) and a link to the cycle page. Future cycles appear when we have rows.
2. Federal vs state: FEC vs secretary of state.
3. Money on federal races is FEC-reported totals, not independent spending (that is a later product).
4. How to vote: we summarize sourced rules; register and find a polling place on vote.gov / SOS.
5. Footer: we are not an elections office or a campaign. Disclaimer.

## SEO

- Unique `<title>` and H1 per path.
- Prerender `/elections`, `/elections/2026`, `/elections/2026/{st}`, federal race paths. District paths prerender in batches or on demand.
- Sitemap those paths.
- Canonical path URLs.

## Error handling

- Missing official field → omit / null, UI says “see the official page.”
- Unknown race slug → 404.
- Invalid state in the path → 404.
- FEC down at ingest time → job fails, hosted pages still show last upsert.
- OpenFEC returns a candidate we cannot attach to a race → skip, log, do not invent a district.
- Member match for incumbent is best-effort; unmatched race still posts.

## Testing

- Catalog: 51 rows, 50 unique codes, official `source_url`, nonempty `sos_portal_url`.
- FEC mapper: fixture candidate → slug, party enum, money fields, `source_url` on fec.gov.
- Single-letter / unsafe slugs rejected the same way as FOIA offices.
- Dry-run posts nothing.

## Out of scope (this pass)

- User accounts, saved races, email alerts
- Voter registration form, polling-place lookup, Google Civic
- Full FEC donor / independent-expenditure / LDA database
- Unofficial live results tickers
- County, city, school-board, and ballot-measure pages
- Invented state-legislative challenger lists
- Presidential 2028 candidate ingest until that cycle’s catalog row exists
- Extra LegiScan keys

## Prompt order for Horizons

1. Collections + ingest + public GET  
2. Hub `/elections` + `/elections/2026` + nav  
3. Federal race pages + candidate/money table  
4. State hubs + district pages + voter-rule block + sitemap / canonicals / prerender  

Paste from `docs/HORIZONS_ELECTIONS.md`.
