"""DB connection + upsert helpers. Assumes Postgres (adjust the connector
and SQL syntax if Horizons provisions a different engine — confirm with
Horizons what it actually gave you before assuming Postgres)."""
from __future__ import annotations

import logging
import re
from contextlib import contextmanager

import psycopg2
import psycopg2.extras

from . import config
from .names import normalize_state, parse_filer_name, parse_state_district

logger = logging.getLogger("rollcalltrack_ingest.db")

# Cached per-process: which optional members columns exist
_members_columns: set[str] | None = None


@contextmanager
def get_conn():
    conn = psycopg2.connect(config.DATABASE_URL)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def upsert_filing(conn, filing: dict) -> int:
    """Insert or update a disclosure_filings row. Returns the filing id.
    Expects keys: member_id, chamber, filing_type, filing_date,
    source_url, raw_document_type, parse_status."""
    sql = """
        INSERT INTO disclosure_filings
            (member_id, chamber, filing_type, filing_date,
             source_url, raw_document_type, parse_status, ingested_at)
        VALUES (%(member_id)s, %(chamber)s, %(filing_type)s, %(filing_date)s,
                %(source_url)s, %(raw_document_type)s, %(parse_status)s, now())
        ON CONFLICT (source_url) DO UPDATE SET
            parse_status = EXCLUDED.parse_status,
            ingested_at = now()
        RETURNING id;
    """
    with conn.cursor() as cur:
        cur.execute(sql, filing)
        return cur.fetchone()[0]


def upsert_transaction(conn, txn: dict) -> None:
    """Insert or update a transactions row, deduped on a natural key of
    (filing_id, asset_name, transaction_date, amount_range_raw) since
    source systems don't always give a stable per-transaction ID."""
    sql = """
        INSERT INTO transactions
            (filing_id, member_id, filer_relation, asset_name, ticker,
             ticker_confidence, transaction_type, transaction_date,
             notification_date, amount_range_raw, amount_range_low,
             amount_range_high, asset_category)
        VALUES (%(filing_id)s, %(member_id)s, %(filer_relation)s,
                %(asset_name)s, %(ticker)s, %(ticker_confidence)s,
                %(transaction_type)s, %(transaction_date)s,
                %(notification_date)s, %(amount_range_raw)s,
                %(amount_range_low)s, %(amount_range_high)s,
                %(asset_category)s)
        ON CONFLICT (filing_id, asset_name, transaction_date, amount_range_raw)
        DO NOTHING;
    """
    with conn.cursor() as cur:
        cur.execute(sql, txn)


def upsert_annual_asset(conn, asset: dict) -> None:
    sql = """
        INSERT INTO annual_assets
            (filing_id, member_id, asset_name, value_range_raw,
             value_range_low, value_range_high, income_type,
             income_amount_range)
        VALUES (%(filing_id)s, %(member_id)s, %(asset_name)s,
                %(value_range_raw)s, %(value_range_low)s,
                %(value_range_high)s, %(income_type)s,
                %(income_amount_range)s)
        ON CONFLICT (filing_id, asset_name) DO UPDATE SET
            value_range_raw = EXCLUDED.value_range_raw,
            value_range_low = EXCLUDED.value_range_low,
            value_range_high = EXCLUDED.value_range_high;
    """
    with conn.cursor() as cur:
        cur.execute(sql, asset)


def log_ingestion_run(conn, source: str, found: int, parsed: int,
                       failed: int, note: str = "") -> None:
    sql = """
        INSERT INTO ingestion_log
            (source, run_at, filings_found, filings_parsed,
             filings_failed, note)
        VALUES (%s, now(), %s, %s, %s, %s);
    """
    with conn.cursor() as cur:
        cur.execute(sql, (source, found, parsed, failed, note))
    logger.info(
        "Logged ingestion run: source=%s found=%d parsed=%d failed=%d",
        source, found, parsed, failed,
    )


def _members_cols(conn) -> set[str]:
    global _members_columns
    if _members_columns is not None:
        return _members_columns
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'members'
            """
        )
        _members_columns = {r[0] for r in cur.fetchall()}
    if not _members_columns:
        # table missing or different schema search_path — assume minimal
        _members_columns = {"id", "full_name"}
        logger.warning(
            "Could not introspect members columns; assuming id + full_name only"
        )
    else:
        logger.debug("members columns: %s", sorted(_members_columns))
    return _members_columns


def find_member_id(
    conn,
    name: str,
    state: str | None = None,
    district: int | None = None,
    chamber: str | None = None,
    office: str | None = None,
    state_district: str | None = None,
) -> int | None:
    """Best-effort match of a disclosure filer name to an existing member.

    Strategies (first unique hit wins):
      1. Exact full_name (case-insensitive) against name variants
      2. Normalized full_name (strip punct/hyphen, collapse space)
      3. first_name / last_name columns if present
      4. last-name equality + first-name prefix (Don → Donald)
      5. Disambiguate multi-matches with state / district / chamber

    Returns None on no match OR ambiguous multi-match so we never attach
    a filing to the wrong member.
    """
    if not name or not str(name).strip():
        return None

    # Allow House StateDst passthrough
    if state_district and (state is None or district is None):
        sc, dist = parse_state_district(state_district)
        state = state or sc
        if district is None:
            district = dist

    state_code = normalize_state(state)
    parsed = parse_filer_name(name, office=office)
    if not parsed.full and not parsed.last:
        return None

    cols = _members_cols(conn)
    if "full_name" not in cols and "first_name" not in cols:
        logger.error("members table has neither full_name nor first_name — cannot match")
        return None

    # Collect candidate rows as (id, full_name_or_label, state, district, chamber)
    candidates: list[tuple] = []

    with conn.cursor() as cur:
        # --- Strategy 1: exact variant match on full_name ---
        if "full_name" in cols:
            for variant in parsed.variants:
                if not variant or len(variant) < 3:
                    continue
                # skip compact no-space keys here (handled in strategy 2)
                if " " not in variant and "," not in variant:
                    continue
                cur.execute(
                    "SELECT id, full_name FROM members WHERE lower(full_name) = lower(%s)",
                    (variant,),
                )
                rows = cur.fetchall()
                if len(rows) == 1:
                    logger.info(
                        "Member match exact: %r → id=%s (%s)",
                        name, rows[0][0], rows[0][1],
                    )
                    return rows[0][0]
                if len(rows) > 1:
                    candidates.extend(rows)

        # --- Strategy 2: normalized equality (strip non-letters) ---
        if "full_name" in cols:
            # Postgres regexp_replace to compare alnum-only forms
            for variant in parsed.variants:
                if not variant:
                    continue
                compact = re.sub(r"[^a-z0-9]", "", variant.lower())
                if len(compact) < 5:
                    continue
                cur.execute(
                    """
                    SELECT id, full_name FROM members
                    WHERE lower(regexp_replace(full_name, '[^a-zA-Z0-9]', '', 'g')) = %s
                    """,
                    (compact,),
                )
                rows = cur.fetchall()
                if len(rows) == 1:
                    logger.info(
                        "Member match normalized: %r → id=%s (%s)",
                        name, rows[0][0], rows[0][1],
                    )
                    return rows[0][0]
                if len(rows) > 1:
                    candidates.extend(rows)

        # --- Strategy 3: dedicated first_name / last_name columns ---
        if "first_name" in cols and "last_name" in cols and parsed.first and parsed.last:
            cur.execute(
                """
                SELECT id, COALESCE(full_name, first_name || ' ' || last_name)
                FROM members
                WHERE lower(last_name) = lower(%s)
                  AND (
                    lower(first_name) = lower(%s)
                    OR lower(first_name) LIKE lower(%s) || '%%'
                    OR lower(%s) LIKE lower(first_name) || '%%'
                  )
                """,
                (parsed.last, parsed.first, parsed.first, parsed.first),
            )
            rows = cur.fetchall()
            if len(rows) == 1:
                logger.info(
                    "Member match first/last cols: %r → id=%s (%s)",
                    name, rows[0][0], rows[0][1],
                )
                return rows[0][0]
            if len(rows) > 1:
                candidates.extend(rows)

        # --- Strategy 4: full_name last-token + first-token prefix ---
        if "full_name" in cols and parsed.first and parsed.last:
            # last name as whole word at end (or before suffix)
            cur.execute(
                """
                SELECT id, full_name FROM members
                WHERE (
                    lower(full_name) ~ ('\\m' || lower(%s) || '\\M\\s*$')
                    OR lower(full_name) LIKE '%% ' || lower(%s)
                    OR lower(split_part(full_name, ' ', array_length(string_to_array(full_name, ' '), 1))) = lower(%s)
                )
                AND (
                    lower(full_name) LIKE lower(%s) || '%%'
                    OR lower(split_part(full_name, ' ', 1)) LIKE lower(%s) || '%%'
                    OR lower(%s) LIKE lower(split_part(full_name, ' ', 1)) || '%%'
                )
                """,
                (
                    parsed.last,
                    parsed.last,
                    parsed.last,
                    parsed.first,
                    parsed.first,
                    parsed.first,
                ),
            )
            rows = cur.fetchall()
            if len(rows) == 1:
                logger.info(
                    "Member match last+first-prefix: %r → id=%s (%s)",
                    name, rows[0][0], rows[0][1],
                )
                return rows[0][0]
            if len(rows) > 1:
                candidates.extend(rows)

    # --- Strategy 5: disambiguate accumulated candidates ---
    if not candidates:
        logger.info(
            "No member match for name=%r state=%s district=%s chamber=%s",
            name, state_code, district, chamber,
        )
        return None

    # Dedupe by id
    by_id: dict[int, str] = {}
    for row in candidates:
        by_id[row[0]] = row[1]

    if len(by_id) == 1:
        mid = next(iter(by_id))
        logger.info(
            "Member match unique candidate: %r → id=%s (%s)",
            name, mid, by_id[mid],
        )
        return mid

    # Pull state/district/chamber for remaining candidates if columns exist
    ids = list(by_id.keys())
    scored: list[tuple[int, int, str]] = []  # (score, id, label)
    select_bits = ["id", "full_name" if "full_name" in cols else "id::text AS full_name"]
    if "state" in cols:
        select_bits.append("state")
    if "district" in cols:
        select_bits.append("district")
    if "chamber" in cols:
        select_bits.append("chamber")

    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {', '.join(select_bits)} FROM members WHERE id = ANY(%s)",
            (ids,),
        )
        detail_rows = cur.fetchall()

    # Map column positions
    col_names = [c.split()[-1] if " AS " not in c.upper() else c.split()[-1]
                 for c in select_bits]
    # simpler: rebuild from select_bits
    colnames = []
    for c in select_bits:
        if " AS " in c.upper():
            colnames.append(c.split()[-1])
        else:
            colnames.append(c)

    for row in detail_rows:
        rowd = dict(zip(colnames, row))
        score = 0
        mid = rowd["id"]
        label = str(rowd.get("full_name") or mid)

        m_state = normalize_state(rowd.get("state")) if "state" in rowd else None
        if state_code and m_state:
            if m_state == state_code:
                score += 10
            else:
                score -= 20  # hard conflict

        if district is not None and "district" in rowd and rowd["district"] is not None:
            try:
                if int(rowd["district"]) == int(district):
                    score += 5
                else:
                    score -= 10
            except (TypeError, ValueError):
                pass

        if chamber and "chamber" in rowd and rowd["chamber"]:
            if str(rowd["chamber"]).lower() == chamber.lower():
                score += 3
            else:
                score -= 5

        scored.append((score, mid, label))

    scored.sort(key=lambda x: x[0], reverse=True)
    if not scored:
        return None

    best_score, best_id, best_label = scored[0]
    # Require a positive disambiguation score and a unique best
    ties = [s for s in scored if s[0] == best_score]
    if best_score > 0 and len(ties) == 1:
        logger.info(
            "Member match disambiguated (score=%d): %r → id=%s (%s)",
            best_score, name, best_id, best_label,
        )
        return best_id

    logger.warning(
        "Ambiguous member match for %r (state=%s district=%s) — candidates=%s; skipping",
        name,
        state_code,
        district,
        [(s[1], s[2], s[0]) for s in scored[:5]],
    )
    return None
