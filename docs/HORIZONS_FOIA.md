# Horizons: FOIA / public records

Split: GitHub ingest (FOIA.gov API + 50-state catalog) writes these collections.
Horizons owns schema, GET/POST, and `/foia` UI.

Auth on ingest: `X-Ingest-Key` (same as `/hcgi/api/ingest/disclosures`).
Public GET: no key.
Do not file requests for users. Do not store requester PII.

See `docs/superpowers/specs/2026-08-23-foia-public-records-design.md`.

---

## Prompt 1 — schema + ingest + public GET

```
Add public-records FOIA collections and APIs. Do not build the /foia UI in this prompt.

Auth: header X-Ingest-Key (same secret as /hcgi/api/ingest/disclosures).
Create/update/delete API-only. List/view public.

COLLECTIONS

1) public_records_jurisdictions
One row for federal and one per USPS state.
- jurisdiction enum [federal, state]
- state_code text nullable, 2-letter USPS, required if state, null if federal, unique when state
- name text required
- statute_citation text
- response_days number nullable
- response_days_unit enum [business, calendar] nullable
- fee_summary text
- portal_url text required
- guidance_url text
- official_form_url text nullable
- official_form_title text nullable
- local_how_to text (state only; empty for federal)
- exemptions_note text
- source_url text required, unique upsert key

Indexes: jurisdiction, state_code, source_url.

2) public_records_offices
Federal FOIA components and statewide offices. No county/city rows.
- slug text required, unique, URL-safe
- jurisdiction enum [federal, state]
- state_code text nullable, 2-letter, required if state
- parent_agency text
- office_name text required
- abbreviation text
- foia_email text nullable
- request_url text
- website text
- reading_room_url text
- submission_methods text
- mail_address text
- official_form_url text nullable
- source_url text required, unique upsert
- foia_gov_id text nullable (FOIA.gov component UUID)

Indexes: slug, jurisdiction, state_code, source_url, parent_agency.

Do NOT create state_foia_* parallel tables.

INGEST (X-Ingest-Key)

POST /hcgi/api/ingest/public-records/jurisdictions
{ "jurisdictions": [ { ...fields } ] }
Upsert on source_url. Re-POST updates. Return { ok, written, updated }.
400 if missing source_url or portal_url, invalid state_code, or jurisdiction=state without state_code.
Ignore unknown extra keys.

POST /hcgi/api/ingest/public-records/offices
{ "offices": [ { ...fields } ] }
Upsert on source_url (also keep slug unique). Same 400 rules. Return { ok, written, updated }.

PUBLIC GET (no key)

GET /hcgi/api/public-records/jurisdictions
Query: state (2-letter), jurisdiction=federal|state
Return { ok, items } including all 50 states + federal even if some fields are null.

GET /hcgi/api/public-records/offices
Query: jurisdiction, state, q (name/abbreviation contains), page, limit
Return { ok, page, limit, total, items }.

GET /hcgi/api/public-records/offices/{slug}
Return { ok, item } or 404.

SUCCESS CHECK
1) POST a federal jurisdiction row (source_url https://www.foia.gov/, portal_url https://www.foia.gov/request) → 200
2) POST a state row AL with state_code=AL → 200; missing state_code → 400
3) POST an office slug fbi, jurisdiction=federal → 200; GET by slug → 200
4) GET jurisdictions includes federal + AL
5) No UI required in this prompt
```

---

## Prompt 2 — hub `/foia`

```
Build /foia as the single FOIA / public records product hub. Federal AND state on this page (toggle), same guided form.

Do not file or email requests. Do not save requester fields on the server.

NAV: add "FOIA / Public records" → /foia (near spending / judicial).

PAGE SECTIONS
1) Search already-public records first. Link relevant site tools (votes, spending, judicial). Then FOIA.
2) Toggle: Federal | State (state picker, 50 + DC not required). Changing toggle updates statute name, deadline, portal CTA, official form button.
3) How it works: written request, reasonably describe records, agencies need not create new records or answer questions. Federal = 5 U.S.C. § 552, typically 20 business days. State = that state's statute and clock from GET /hcgi/api/public-records/jurisdictions.
4) Fees, fee waiver, nine federal exemptions in plain English. State: exemptions differ; point at statute.
5) Appeal, then court. Footer: we are not a law firm; this is a request letter helper; submit on the official portal.

GUIDED FORM (client-side)
name, mailing address, email, phone, agency (search GET /hcgi/api/public-records/offices filtered by toggle), records description, date range, fee category [media, education, commercial, other], fee cap dollars, fee-waiver checkbox + reason, date.

LIVE OUTPUTS on the same page
- Letter preview + copy
- Download PDF
- Download DOCX
- If jurisdiction/office has official_form_url: button "Official form" (new tab)
- Primary CTA: Federal → https://www.foia.gov/request  State → that row's portal_url
  Label: "Submit on FOIA.gov" / "Open {state} public records portal"

Letter text must include the statute name, specific records, requester identity/contact, fee category, fee cap or waiver.

PDF/DOCX generated in the browser from the letter. No PII POST to our API.

Load jurisdictions from GET /hcgi/api/public-records/jurisdictions. If empty, still show federal explainer and FOIA.gov CTA.

SEO: title "FOIA and public records requests — Roll Call Track". Unique H1. Prerender /foia.

Query aliases: /foia?state=al and /foia?agency=fbi 301 to /foia/al and /foia/federal/fbi once those routes exist. Until then, apply the filter on /foia without 404.

SUCCESS: /foia renders, toggle switches federal vs AL copy, form fills letter, PDF downloads, Submit does not POST to our origin.
```

---

## Prompt 3 — federal office URLs

```
Add /foia/federal/{slug} using GET /hcgi/api/public-records/offices/{slug} (jurisdiction=federal).

Same chrome and guided form as /foia, agency pre-selected, letter/PDF/DOCX work.

Show: parent agency, office name, reading room, request_url, foia_email, submission_methods, mail_address, official form if any.
CTA: Submit on FOIA.gov (https://www.foia.gov/request) and office request_url if different.

On /foia federal mode: searchable list of offices (GET /hcgi/api/public-records/offices?jurisdiction=federal&q=) linking to /foia/federal/{slug}.

404 if slug missing. Unique title "{office_name} FOIA — Roll Call Track".
Prerender and sitemap all federal slugs from the GET list.

Canonical: path URL, not query string.
```

---

## Prompt 4 — state hubs + statewide offices + sitemap

```
Add:
- /foia/{state}  state = 2-letter lower (al..wy)
- /foia/{state}/{agency}  agency = office slug without state prefix or the offices.slug

Hub /foia state mode: 50-state index (name, statute, response_days if present) linking to /foia/{st}.

State hub content from GET /hcgi/api/public-records/jurisdictions?state=XX
- statute name + citation
- deadline (only if response_days present; else "see statute")
- fee_summary
- portal CTA
- official form download if official_form_url
- local_how_to block (counties/cities: how to ask under THIS statute; no invented local emails)
- list statewide offices for that state_code → /foia/{st}/{slug}

Office page: same form pre-filled, contacts from GET offices/{slug}.

Invalid state 404. Same chrome/form as /foia.

301: /foia?state=AL → /foia/al
Sitemap + prerender: /foia, /foia/al..wy, federal slugs, statewide office paths.

Nav already has FOIA / Public records from prompt 2.

SUCCESS: /foia/al is a real page (not the federal homepage title), shows Alabama statute from the API, form still generates PDF, portal link is the official URL from the jurisdiction row.
```
