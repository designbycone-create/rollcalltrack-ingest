# Horizons: `/hcgi/api/ingest/judicial/disclosures`

Live `POST https://rollcalltrack.com/hcgi/api/ingest/judicial/disclosures`
returns `404 {"error":"Route not found"}` as of 2026-08-14.

Same shape as `/hcgi/api/ingest/disclosures`: one payload carries the
filing plus its transactions and/or annual assets. Upsert on
`filing_id` (JEFS document id). `source_url` is required on the filing
and on every child row.

`GET /hcgi/api/judicial/judges` is also 404. Until that read route
exists the job loads the FJC export and uses `nid` as `judge_id`.

## Request

`POST /hcgi/api/ingest/judicial/disclosures`  
Header: `X-Ingest-Key`

```json
{
  "filings": [
    {
      "judge_id": "1377101",
      "filing_id": "ptr-1",
      "filing_type": "periodic_transaction",
      "filing_date": "2026-01-20",
      "source_url": "https://pub.jefs.uscourts.gov/?filing=ptr-1",
      "transactions": [
        {
          "filing_id": "ptr-1",
          "asset_name": "Apple Inc. (AAPL)",
          "transaction_type": "purchase",
          "transaction_date": "2026-01-15",
          "amount_range_low": 15001,
          "amount_range_high": 50000,
          "source_url": "https://pub.jefs.uscourts.gov/?filing=ptr-1"
        }
      ],
      "annual_assets": []
    }
  ]
}
```

## Rules

- `judge_id` required — FJC `nid` from fjc-judges-sync
- `filing_id` required; unique upsert key (JEFS `id`)
- `source_url` required on filing, transactions, annual_assets
- `filing_type` enum: `annual`, `periodic_transaction`
- `transaction_type` enum: `purchase`, `sale`, `exchange`
- `amount_range_low` / `_high` and `value_range_low` / `_high` are
  disclosed brackets. `high` may be null when the source is open-ended
  (`P4` / "More than $50,000,000"). Never store a midpoint.
- Reject missing ids/urls or bad enums with 400

## Paste-ready Horizons prompt

```
Add POST /hcgi/api/ingest/judicial/disclosures authenticated by X-Ingest-Key
(same key as /hcgi/api/ingest/disclosures).

Accept { "filings": [ { judge_id, filing_id, filing_type, filing_date,
source_url, transactions[], annual_assets[] } ] }.

filing_type enum [annual, periodic_transaction].
transactions[]: filing_id, asset_name, transaction_type enum
[purchase, sale, exchange], transaction_date (YYYY-MM-DD),
amount_range_low, amount_range_high (nullable), source_url.
annual_assets[]: filing_id, asset_name, asset_type (nullable),
value_range_low, value_range_high (nullable), source_url.

judge_id is the FJC nid. Upsert on filing_id.
Reject missing judge_id/filing_id/source_url or bad enums with 400.
Do not invent dollar midpoints.

Also add GET /hcgi/api/judicial/judges (public or same key) returning
{ judges: [{ judge_id, full_name, court, court_type, status }] }
so the disclosure job can stop re-downloading the FJC CSV.

Return { ok, written, updated }.
```
