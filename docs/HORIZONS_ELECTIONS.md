# Horizons: Elections / midterms

Split: GitHub ingest (OpenFEC + 50-state catalog + members-derived district races) writes these collections.
Horizons owns schema, GET/POST, and `/elections` UI.

Auth on ingest: `X-Ingest-Key` (same as `/hcgi/api/ingest/disclosures`).
Public GET: no key.
Do not register voters. Do not take donations. Do not store requester PII.

See `docs/superpowers/specs/2026-08-24-elections-design.md`.

---

## Prompt 1 — schema + ingest + public GET

```
Add elections collections and APIs. Do not build the /elections UI in this prompt.

Auth: header X-Ingest-Key (same secret as /hcgi/api/ingest/disclosures).
Create/update/delete API-only. List/view public.

COLLECTIONS

1) elections_jurisdictions
One row for federal and one per USPS state (voter rules + official portals + dates).
- jurisdiction enum [federal, state]
- state_code text nullable, 2-letter USPS, required if state, null if federal, unique when state
- name text required
- sos_portal_url text required
- vote_gov_url text nullable
- registration_url text nullable
- registration_deadline date nullable
- registration_deadline_note text
- voter_id_note text
- absentee_note text
- primary_date date nullable
- runoff_date date nullable
- general_date date nullable
- source_url text required, unique upsert key

Indexes: jurisdiction, state_code, source_url.

2) elections_races
One row per seat per cycle. A special election for the same seat is a separate row (is_special true, distinct slug). No county/city races.
- slug text required, unique, URL-safe lowercase letters/digits/hyphens, min length 2
- cycle number required (even year, e.g. 2026)
- jurisdiction enum [federal, state]
- state_code text nullable, 2-letter, required unless office=us_president
- office enum [us_president, us_senate, us_house, governor, lieutenant_governor, attorney_general, secretary_of_state, treasurer, auditor, other_statewide, state_senate, state_house, unicameral]
- district text nullable (unpadded number like "21" or "3"; empty for statewide/senate/president)
- office_name text required
- is_special bool default false
- primary_date date nullable
- runoff_date date nullable
- general_date date nullable
- incumbent_name text nullable
- incumbent_bioguide_id text nullable
- is_term_up bool nullable
- sos_ballot_url text nullable
- certified_winner text nullable
- result_source_url text nullable
- source_url text required, unique upsert

Indexes: slug, cycle, jurisdiction, state_code, office, source_url.

Slug examples (must accept these):
- 2026-tx-us-senate
- 2026-tx-us-house-21
- 2026-tx-us-house-21-special
- 2026-tx-governor
- 2026-tx-sd-3
- 2026-tx-hd-12
- 2026-ne-leg-5
- 2028-us-president

3) elections_candidates
Federal FEC candidates. State legislative challengers are not stored in v1.
- race_slug text required
- cycle number required
- state_code text nullable, 2-letter
- fec_candidate_id text required
- name text required
- party enum [D, R, I, N, O]
- party_raw text
- is_incumbent bool
- bioguide_id text nullable
- receipts number nullable
- disbursements number nullable
- cash_on_hand number nullable
- coverage_end_date date nullable
- source_url text required, unique upsert

Indexes: race_slug, cycle, state_code, fec_candidate_id, source_url.

Do NOT create state_elections_* parallel tables. Do NOT create an elections_cycles collection (cycle is a field on races/candidates).

INGEST (X-Ingest-Key)

POST /hcgi/api/ingest/elections/jurisdictions
{ "jurisdictions": [ { ...fields } ] }
Upsert on source_url. Re-POST updates. Return { ok, written, updated }.
400 if missing source_url or sos_portal_url, invalid state_code, or jurisdiction=state without state_code.
Ignore unknown extra keys.

POST /hcgi/api/ingest/elections/races
{ "races": [ { ...fields } ] }
Upsert on slug (source_url also unique). Same 400 rules. Also 400 if missing slug, cycle, office_name, or invalid office enum. Return { ok, written, updated }.

POST /hcgi/api/ingest/elections/candidates
{ "candidates": [ { ...fields } ] }
Upsert on source_url. 400 if missing source_url, race_slug, fec_candidate_id, name, or invalid party. Return { ok, written, updated }.

PUBLIC GET (no key)

GET /hcgi/api/elections/jurisdictions
Query: state (2-letter), jurisdiction=federal|state
Return { ok, items } including all 50 states + federal even if some fields are null.

GET /hcgi/api/elections/races
Query: cycle, jurisdiction, state, office, q (office_name/slug contains), page, limit
Return { ok, page, limit, total, items }.

GET /hcgi/api/elections/races/{slug}
Return { ok, item } or 404.

GET /hcgi/api/elections/candidates
Query: race (race_slug), cycle, state, page, limit
Return { ok, page, limit, total, items }.

SUCCESS CHECK
1) POST a federal jurisdiction row (source_url https://www.fec.gov/, sos_portal_url https://www.fec.gov/, general_date 2026-11-03) → 200
2) POST a state row TX with state_code=TX → 200; missing state_code → 400
3) POST a race slug 2026-tx-us-senate, jurisdiction=federal, office=us_senate, cycle=2026, source_url https://www.fec.gov/data/elections/senate/TX/2026/ → 200; GET by slug → 200
4) POST a candidate for that race_slug with fec_candidate_id, party=R, receipts=1 → 200; GET /hcgi/api/elections/candidates?race=2026-tx-us-senate includes it
5) GET jurisdictions includes federal + TX
6) No UI required in this prompt
```

---

## Prompt 2 — hub `/elections` + cycle page

```
Build /elections as the single Elections product hub, plus /elections/2026 as the 2026 cycle page. Federal AND state on the hub (toggle).

Do not register voters. Do not take donations. Do not save visitor fields on the server.

NAV: add "Elections" → /elections (near members / votes / FOIA).

PAGE SECTIONS (/elections)
1) This cycle: 2026 midterms. Federal general Tuesday, November 3, 2026 (only state this as fact from FEC; if jurisdictions.general_date is present, use that). Link to /elections/2026.
2) Toggle: Federal | State (state picker, 50; DC not required).
3) Federal mode: House / Senate / specials from GET /hcgi/api/elections/races?cycle=2026&jurisdiction=federal. Link each row to /elections/2026/{state}/{race-key} where race-key is the slug with the "2026-{state}-" prefix stripped (e.g. slug 2026-tx-us-house-21 → /elections/2026/tx/us-house-21). If those routes 404 because prompt 3 is not done yet, keep the table on this page without 404ing the hub.
4) State mode: 50-state index from GET /hcgi/api/elections/jurisdictions?jurisdiction=state. Each state links to /elections/2026/{st}. Until prompt 4, linking may land on a stub that still uses this chrome.
5) Footer: we are not an elections office or a campaign; register and find a polling place on vote.gov / the secretary of state. Certified results appear after the official source publishes them — no live unofficial ticker.

If GET returns empty, still render the explainer, Nov 3 2026 date, vote.gov CTA (https://www.vote.gov/), and FEC CTA (https://www.fec.gov/).

/elections/2026
Same chrome. Title/H1 for the 2026 cycle. Same toggle. Canonical for this cycle.

SEO: title "2026 elections — Roll Call Track". Unique H1. Prerender /elections and /elections/2026.
Query aliases: /elections?year=2026 301 to /elections/2026. /elections?state=tx 301 to /elections/2026/tx once that route exists; until then filter on the hub without 404.

SUCCESS: /elections renders, toggle switches federal vs state, empty API still shows vote.gov + FEC links, no registration form, no donation form.
```

---

## Prompt 3 — federal race URLs

```
Add /elections/{cycle}/{state}/{race} for federal offices using GET /hcgi/api/elections/races/{slug} where slug = "{cycle}-{state}-{race}".

Examples:
- /elections/2026/tx/us-senate → slug 2026-tx-us-senate
- /elections/2026/tx/us-house-21 → slug 2026-tx-us-house-21
- /elections/2026/tx/us-house-21-special → slug 2026-tx-us-house-21-special

Only render this template when item.jurisdiction=federal and item.office is us_senate, us_house, or us_president. (State offices come in prompt 4; if a state slug hits this route early, still 200 if the race exists.)

Show: office_name, cycle, dates if present, incumbent_name + link to /members if incumbent_bioguide_id exists, is_term_up (if null, omit), is_special, sos_ballot_url if present.

Candidate table from GET /hcgi/api/elections/candidates?race={slug}
Columns: name, party, incumbent, receipts, disbursements, cash on hand, coverage_end_date, FEC source_url (new tab), member link when bioguide_id present.
If no candidates: "Candidate list from the FEC will appear here after ingest. Official filings: FEC.gov."

CTA: Open FEC.gov (https://www.fec.gov/) and the race/candidate source_url. Do not take donations.

On /elections federal mode: races link to these paths.

404 if slug missing. Unique title "{office_name} {cycle} — Roll Call Track".
Prerender and sitemap federal slugs from GET /hcgi/api/elections/races?cycle=2026&jurisdiction=federal.

Canonical: path URL, not query string.
```

---

## Prompt 4 — state hubs + district pages + sitemap

```
Add:
- /elections/{cycle}/{state}  state = 2-letter lower (al..wy)
- /elections/{cycle}/{state}/{race}  for remaining office keys (governor, lt-governor, ag, sos, treasurer, auditor, sd-N, hd-N, leg-N, statewide-*)

State hub content from GET /hcgi/api/elections/jurisdictions?state=XX
- name
- general / primary / runoff dates only if present; else "see the official election calendar"
- voter_id_note, absentee_note, registration_deadline / registration_deadline_note (omit if empty; never invent)
- CTAs: sos_portal_url ("Open {state} election site"), vote_gov_url or https://www.vote.gov/, registration_url if present
- Ballot map from GET /hcgi/api/elections/races?cycle={cycle}&state=XX grouped by office: U.S. Senate, U.S. House, statewide, state senate, state house / unicameral. Each row links to /elections/{cycle}/{st}/{race-key}.

District / statewide page (non-federal or federal already in prompt 3):
- office_name, incumbent, is_term_up if not null, dates if present
- If elections_candidates for this slug is empty (expected for state legislature): do NOT invent names. Show incumbent from the race row and CTA sos_ballot_url or jurisdiction sos_portal_url labeled "Official candidate list / sample ballot"
- If candidates exist (federal), same table as prompt 3

Invalid state 404. Invalid cycle that has no races: 404 or empty cycle page with a link back to /elections — pick 404 if cycle is not a 4-digit even year.

301: /elections?state=TX → /elections/2026/tx (default cycle = latest cycle present, else 2026)
Sitemap + prerender: /elections, /elections/2026, /elections/2026/al..wy, federal race paths. District paths: include in sitemap from the races GET; prerender in batches if a full prerender is too large.

Nav already has Elections from prompt 2.

SUCCESS: /elections/2026/tx is a real page (not the federal hub title), shows Texas voter-rule fields from the API when present, lists race links, a state senate slug shows incumbent + official SOS link and does not invent challengers.
```
