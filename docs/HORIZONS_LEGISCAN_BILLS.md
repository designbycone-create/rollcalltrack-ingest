# Horizons: state bills, votes, coverage

Live (2026-08-14):

| Route | Status |
|-------|--------|
| `POST /hcgi/api/ingest/bills` | **404** — needs to be added (or existing bills ingest enabled) |
| `POST /hcgi/api/ingest/votes` | exists (401 without key) |
| `POST /hcgi/api/ingest/state-coverage` | **404** — new collection + route |

Same rule as members: **no** `state_bills` / `state_votes` tables. Add
`jurisdiction` + `state_code` to the existing collections.

## Collision warning

`bills` currently uniques on `(congress, bill_type, bill_number)`.
`TX HB1` and `CA HB1` with `congress=2025` (session year) will collide.
Change the unique key to include `state_code` (and `jurisdiction`), or
upsert only on `id` / `source_url`. Worker `id` is `LS{legiscan_bill_id}`.

Vote `id` is `{state}-{year}-{roll_call_id}` (e.g. `de-2025-1574346`).
`rc_id` is `LSRC{roll_call_id}`. Member votes use `bioguide_id=LS{people_id}`.

## Bills body

```json
{
  "bills": [{
    "id": "LS1898133",
    "congress": 2025,
    "bill_type": "B",
    "bill_number": "HB1",
    "title": "An Act … Veterans Affairs.",
    "legislation_status": "Passed",
    "became_law": true,
    "latest_action_text": "Signed by Governor",
    "latest_action_date": "2025-08-13",
    "description": "…",
    "jurisdiction": "state",
    "state_code": "DE",
    "source_url": "https://legiscan.com/DE/bill/HB1/2025"
  }]
}
```

`congress` is the session `year_start` for state rows (not a US Congress
number). Federal rows stay real congress numbers.

## Votes body

```json
{
  "votes": [{
    "id": "de-2025-1574346",
    "rc_id": "LSRC1574346",
    "congress": 2025,
    "chamber": "house",
    "session": "2025",
    "roll_number": "1574346",
    "bill_label": "HB1",
    "title": "House Third Reading",
    "result": "Passed",
    "date": "2025-05-20",
    "tally_yea": 38,
    "tally_nay": 0,
    "tally_present": 0,
    "tally_not_voting": 3,
    "jurisdiction": "state",
    "state_code": "DE",
    "source_url": "https://legiscan.com/DE/rollcall/HB1/id/1574346",
    "member_votes": [
      {"rc_id": "LSRC1574346", "bioguide_id": "LS4411", "vote": "yea"}
    ]
  }]
}
```

Join legislators on `bioguide_id` (`LS{people_id}`). Do not fuzzy-match
names. A missing `people_id` is dropped, not guessed.

## Coverage (new)

Collection `state_coverage`, upsert on `state_code`.

`POST /hcgi/api/ingest/state-coverage`  
Header: `X-Ingest-Key`

```json
{
  "coverage": [{
    "state_code": "DE",
    "legislators_last_synced": "2026-08-14T21:00:00Z",
    "bills_votes_last_synced": "2026-08-14T21:05:00Z",
    "completeness_note": "full session data available; incremental window has bills and roll calls",
    "last_error": null
  }]
}
```

Partial updates: a legislators run may send only `legislators_last_synced`;
a bills run may send only `bills_votes_last_synced`. Merge, do not null
out the other timestamp.

## Paste-ready Horizons prompt

```
1) POST /hcgi/api/ingest/bills already 404 — implement it on the existing
   bills collection. Accept jurisdiction=state, state_code (2-letter),
   source_url required. Upsert on id (LS{bill_id}) or
   (jurisdiction, state_code, bill_number). Do NOT unique only
   (congress, bill_type, bill_number) — that collides across states.

2) POST /hcgi/api/ingest/votes exists. Accept jurisdiction=state,
   state_code, source_url, nested member_votes[] with bioguide_id=LS{people_id}.

3) New collection state_coverage + POST /hcgi/api/ingest/state-coverage.
   Fields: state_code unique, legislators_last_synced, bills_votes_last_synced
   (ISO timestamps, nullable), completeness_note text, last_error text nullable.
   Merge partial updates. Not public-facing required.
```
