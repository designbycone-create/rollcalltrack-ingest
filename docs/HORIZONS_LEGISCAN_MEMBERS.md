# Horizons: `POST /hcgi/api/ingest/members` (state rows)

Live `POST https://rollcalltrack.com/hcgi/api/ingest/members` exists
(401 without a key — not a 404). It must accept the existing members
shape **plus** `jurisdiction` and `state_code`. Do not add a
`/ingest/state-members` route.

Federal party on the public feed is `D` / `R` / `I`. State rows use the
same three codes. Chamber for state is `house` / `senate` / `unicameral`
(Nebraska).

`bioguide_id` for state legislators is `LS{legiscan_people_id}` so it
cannot collide with a congressional bioguide.

## Request

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

If this POST 400s on `jurisdiction`, `state_code`, `chamber=unicameral`,
or a 2-letter `state`, that is a schema addition on the existing
`members` collection — not a new endpoint.

## Paste-ready Horizons note

```
Extend members (and POST /hcgi/api/ingest/members) with:
  jurisdiction enum [federal, state]  (existing rows = federal)
  state_code text nullable (2-letter USPS; null for federal)

Accept chamber=unicameral in addition to house/senate (or House/Senate).
Accept party D / R / I / N. N is Nonpartisan (Nebraska Unicameral),
not Independent. Federal rows stay D/R/I.
Upsert on bioguide_id. State legislators use LS{people_id}.
source_url required.
Reject unknown jurisdiction/chamber/party with 400.
Do not create a state_members collection.
```
