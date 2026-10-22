# Horizons server fixes (copy into Horizons chat / apply to apps/api)

Live-proven API as of 2026-08-05. External worker is `designbycone-create/rollcalltrack-ingest`.

## 1. Member lookup — multi-format names

**File:** `apps/api/src/routes/members-lookup.js`

Replace lookup logic so scrapers can pass **First Last** while stored names are **Last, First**:

```js
// GET /hcgi/api/members/lookup?bioguide_id=… | name=…&state=…&district=…

async function lookupMember(pb, { bioguide_id, name, state, district }) {
  // 1) bioguide exact (fast path)
  if (bioguide_id) {
    try {
      const member = await pb.collection('members').getFirstListItem(
        `bioguide_id = "${escapeFilter(bioguide_id)}"`
      );
      return { found: true, member: shape(member) };
    } catch {
      return { found: false, members: [] };
    }
  }

  if (!name || !String(name).trim()) {
    return {
      found: false,
      error: 'Provide either "bioguide_id" (exact) or "name" (with optional state/district)',
    };
  }

  const candidates = nameCandidates(name);
  for (const cand of candidates) {
    try {
      // case-insensitive contains; tighten with state/district below
      const list = await pb.collection('members').getList(1, 20, {
        filter: `name ~ "${escapeFilter(cand)}"`,
      });
      const hits = (list.items || []).filter((m) =>
        matchesGeo(m, state, district)
      );
      const exact = hits.find(
        (m) => (m.name || '').toLowerCase() === cand.toLowerCase()
      );
      if (exact) return { found: true, member: shape(exact) };
      if (hits.length === 1) return { found: true, member: shape(hits[0]) };
    } catch {
      /* try next candidate */
    }
  }

  return {
    found: false,
    members: [],
    hint: 'No members matched — check spelling or try bioguide_id',
  };
}

function nameCandidates(name) {
  const raw = String(name).trim();
  const out = [];
  const add = (s) => {
    s = (s || '').trim();
    if (s && !out.includes(s)) out.push(s);
  };
  add(raw);
  // "Last, First Middle" already
  if (raw.includes(',')) {
    const [last, rest] = raw.split(',').map((s) => s.trim());
    if (last && rest) add(`${rest} ${last}`);
  } else {
    const parts = raw.split(/\s+/).filter(Boolean);
    if (parts.length >= 2) {
      const last = parts[parts.length - 1];
      const first = parts.slice(0, -1).join(' ');
      add(`${last}, ${first}`);
      add(`${last}, ${parts[0]}`);
      if (parts.length >= 3) {
        // McClain Delaney, April
        add(`${parts[parts.length - 2]} ${parts[parts.length - 1]}, ${parts[0]}`);
      }
    }
  }
  return out;
}

function matchesGeo(m, state, district) {
  if (state) {
    const ms = String(m.state || '').toUpperCase();
    const want = String(state).toUpperCase();
    if (ms !== want && !ms.startsWith(want)) return false;
  }
  if (district !== undefined && district !== null && district !== '') {
    const d = Number(district);
    if (!Number.isNaN(d) && m.district != null && Number(m.district) !== d) {
      // district 0 = senate/at-large: allow
      if (Number(m.district) !== 0) return false;
    }
  }
  return true;
}

function shape(m) {
  return {
    id: m.id,
    bioguide_id: m.bioguide_id,
    name: m.name,
    party: m.party,
    state: m.state,
    district: m.district,
    chamber: m.chamber,
  };
}

function escapeFilter(s) {
  return String(s).replace(/\\/g, '\\\\').replace(/"/g, '\\"');
}
```

**Success check after deploy:**
```
GET /hcgi/api/members/lookup?name=April+McClain+Delaney&state=MD&district=6
→ found: true

GET /hcgi/api/members/lookup?name=Shelley+Moore+Capito
→ found: true
```

---

## 2. Ingest idempotency — `processSinglePayload`

**File:** `apps/api/src/routes/ingest-disclosures.js`

```js
async function processSinglePayload(body, pb) {
  const type = body.type;
  if (!type) throw badRequest('"type" is required');

  if (type === 'filing') {
    const block = body.payload || {};
    const filing = block.filing || block;
    const transactions = block.transactions || [];
    const annual_assets = block.annual_assets || [];

    if (!filing?.source_url) {
      throw badRequest('payload.filing.source_url is required');
    }

    // Idempotent upsert by source_url
    let existing = null;
    try {
      existing = await pb
        .collection('disclosure_filings')
        .getFirstListItem(`source_url = "${escapeFilter(filing.source_url)}"`);
    } catch {
      existing = null;
    }

    const filingData = sanitizeFiling(filing);
    let filingId;
    if (existing) {
      filingId = existing.id;
      await pb.collection('disclosure_filings').update(filingId, filingData);
      // Replace child rows so retries don't double-count
      const oldTxns = await pb.collection('transactions').getFullList({
        filter: `filing_id = "${filingId}"`,
      });
      for (const t of oldTxns) {
        await pb.collection('transactions').delete(t.id);
      }
      const oldAssets = await pb.collection('annual_assets').getFullList({
        filter: `filing_id = "${filingId}"`,
      });
      for (const a of oldAssets) {
        await pb.collection('annual_assets').delete(a.id);
      }
    } else {
      const created = await pb.collection('disclosure_filings').create(filingData);
      filingId = created.id;
    }

    let transactions_written = 0;
    for (const t of transactions) {
      await pb.collection('transactions').create({
        ...sanitizeTransaction(t),
        filing_id: filingId,
        member_id: filingData.member_id || t.member_id || '',
      });
      transactions_written += 1;
    }

    let annual_assets_written = 0;
    for (const a of annual_assets) {
      await pb.collection('annual_assets').create({
        ...a,
        filing_id: filingId,
        member_id: filingData.member_id || a.member_id || '',
      });
      annual_assets_written += 1;
    }

    return {
      ok: true,
      filing_id: filingId,
      transactions_written,
      annual_assets_written,
      updated: Boolean(existing),
    };
  }

  if (type === 'ingestion_log') {
    const p = body.payload || body;
    const created = await pb.collection('ingestion_log').create(sanitizeLog(p));
    return { ok: true, id: created.id };
  }

  if (type === 'review_queue' || type === 'manual_review') {
    const p = body.payload || body;
    const created = await pb.collection('manual_review_queue').create(sanitizeReview(p));
    return { ok: true, id: created.id };
  }

  throw badRequest(
    `Unknown type "${type}". Must be one of: "filing", "ingestion_log", "review_queue" (alias: "manual_review")`
  );
}
```

Map enums server-side as a safety net:
- `sale_partial` → `sale`
- `dependent_child` → `dependent`
- `ticker_confidence`: `filed`→`high`, `unconfirmed`→`low`
- On validation failure return **400** with field details, not 500

---

## 3. Expected external client body (already used successfully)

```http
POST /hcgi/api/ingest/disclosures
X-Ingest-Key: <secret>
Content-Type: application/json
```

```json
{
  "type": "filing",
  "payload": {
    "filing": {
      "member_id": "61kvydvhn8votlk",
      "bioguide_id": "M001232",
      "chamber": "house",
      "filing_type": "ptr",
      "filing_date": "8/3/2026",
      "source_url": "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/20035118.pdf",
      "raw_document_type": "pdf",
      "parse_status": "parsed",
      "member_name": "McClain Delaney, April",
      "member_party": "D",
      "member_state": "MD"
    },
    "transactions": [
      {
        "asset_name": "BWX Technologies, Inc. Common Stock (BWXT)",
        "filer_relation": "dependent",
        "transaction_type": "purchase",
        "transaction_date": "07/24/2026",
        "notification_date": "08/01/2026",
        "amount_range_raw": "$15,001 - $50,000",
        "ticker": "BWXT",
        "ticker_confidence": "medium",
        "line_index": 0
      }
    ]
  }
}
```

---

Transaction identity is `filing_id` + `line_index` (0-based position in the
filing). A PTR can list the same asset, date, and amount twice; those are two
rows. Persist `line_index`, `ticker`, and `ticker_confidence` from the
payload. `ticker` may be an empty string when the form has no symbol.

## 4. Success criteria after you ship

1. First Last lookup works for Delaney + Capito (no client workaround required).
2. Re-POST same `source_url` → same `filing_id`, `updated: true`.
3. No empty duplicate Capito rows on retry.
4. GitHub worker needs only `HORIZONS_BASE_URL` + `INGEST_API_KEY`.
