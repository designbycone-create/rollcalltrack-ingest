"""PocketBase collection helpers for Horizons rollcalltrack.

Live schema (2026-08) collections used here:
  members, disclosure_filings, transactions, annual_assets,
  ingestion_log, manual_review_queue

This module does not authenticate. Superuser and admin password login
have been removed. Ingest writes use the Horizons API key.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import requests

from . import config
from .guards import assert_remote_post_allowed, skip_reason
from .names import normalize_state, parse_filer_name, parse_state_district

logger = logging.getLogger("rollcalltrack_ingest.db")

# PB select enums (from live collection schema)
_PARSE_STATUS = {"pending", "parsed", "failed"}
_FILING_TYPES = {"annual", "ptr", "new_filer", "termination", "amendment"}
_TXN_TYPES = {"purchase", "sale", "exchange", "other"}
_FILER_REL = {"self", "spouse", "dependent", "joint"}
_TICKER_CONF = {"high", "medium", "low", "none"}
_SOURCES = {"house_clerk", "senate_efd", "congress_gov"}


class PocketBase:
    """Minimal PocketBase REST client. Does not log in."""

    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers["User-Agent"] = config.USER_AGENT

    def _url(self, collection: str, record_id: str | None = None) -> str:
        path = f"{self.base}/api/collections/{collection}/records"
        if record_id:
            path += f"/{record_id}"
        return path

    def list(
        self,
        collection: str,
        *,
        filter: str | None = None,
        sort: str | None = None,
        page: int = 1,
        per_page: int = 50,
    ) -> dict:
        params: dict[str, Any] = {"page": page, "perPage": per_page}
        if filter:
            params["filter"] = filter
        if sort:
            params["sort"] = sort
        resp = self.session.get(
            self._url(collection),
            params=params,
            timeout=config.REQUEST_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        return resp.json()

    def create(self, collection: str, body: dict) -> dict:
        assert_remote_post_allowed(self.base)
        resp = self.session.post(
            self._url(collection),
            json=body,
            timeout=config.REQUEST_TIMEOUT_SECONDS,
        )
        if not resp.ok:
            logger.error(
                "PB create %s failed %s: %s body=%s",
                collection, resp.status_code, resp.text[:500], body,
            )
            resp.raise_for_status()
        return resp.json()

    def update(self, collection: str, record_id: str, body: dict) -> dict:
        assert_remote_post_allowed(self.base)
        resp = self.session.patch(
            self._url(collection, record_id),
            json=body,
            timeout=config.REQUEST_TIMEOUT_SECONDS,
        )
        if not resp.ok:
            logger.error(
                "PB update %s/%s failed %s: %s",
                collection, record_id, resp.status_code, resp.text[:500],
            )
            resp.raise_for_status()
        return resp.json()

    def find_one(self, collection: str, filter: str) -> dict | None:
        data = self.list(collection, filter=filter, per_page=1)
        items = data.get("items") or []
        return items[0] if items else None


def _escape_filter(value: str) -> str:
    """Escape a string for PB filter expressions."""
    return (value or "").replace("\\", "\\\\").replace('"', '\\"')


def _map_filing_type(raw: str | None) -> str:
    if not raw:
        return "ptr"
    s = raw.strip().lower()
    if s in _FILING_TYPES:
        return s
    # House code letter
    if raw.strip().upper() in config.HOUSE_TO_PB_FILING_TYPE:
        return config.HOUSE_TO_PB_FILING_TYPE[raw.strip().upper()]
    if "ptr" in s or "periodic" in s or "transaction" in s:
        return "ptr"
    if "annual" in s:
        return "annual"
    if "termin" in s:
        return "termination"
    if "amend" in s:
        return "amendment"
    if "new" in s or "candidate" in s:
        return "new_filer"
    return "ptr"


def _map_parse_status(raw: str | None) -> str:
    s = (raw or "parsed").strip().lower()
    if s in _PARSE_STATUS:
        return s
    if s in ("empty", "no_text_layer", "paper_unparsed", "ocr_fallback"):
        return "failed"
    return "parsed"


def _map_txn_type(raw: str | None) -> str:
    s = (raw or "other").strip().lower()
    if s in _TXN_TYPES:
        return s
    if "partial" in s or s.startswith("sale"):
        return "sale"
    if s.startswith("purch"):
        return "purchase"
    if s.startswith("exch"):
        return "exchange"
    return "other"


def _map_filer_relation(raw: str | None) -> str:
    s = (raw or "self").strip().lower()
    if s in _FILER_REL:
        return s
    if "spouse" in s:
        return "spouse"
    if "joint" in s:
        return "joint"
    if "child" in s or "dependent" in s:
        return "dependent"
    return "self"


def _map_ticker_conf(raw: str | None) -> str:
    s = (raw or "none").strip().lower()
    if s in _TICKER_CONF:
        return s
    if s in ("filed", "confirmed", "exact"):
        return "high"
    if s == "unconfirmed":
        return "low"
    return "none"


def _map_source(raw: str) -> str:
    s = (raw or "").strip().lower()
    if s in _SOURCES:
        return s
    if s in ("house", "house_clerk"):
        return "house_clerk"
    if s in ("senate", "senate_efd"):
        return "senate_efd"
    if "congress" in s:
        return "congress_gov"
    return "house_clerk"


def upsert_filing(conn: PocketBase, filing: dict) -> str:
    """Insert or update disclosure_filings by source_url. Returns record id."""
    source_url = filing.get("source_url") or ""
    if not source_url:
        raise ValueError("source_url is required — refuse to store filing without origin link")
    if skip_reason(filing, kind="filing", require_name=True):
        return ""

    body = {
        "chamber": filing.get("chamber") if filing.get("chamber") in ("house", "senate") else None,
        "filing_type": _map_filing_type(filing.get("filing_type")),
        "filing_date": filing.get("filing_date") or "",
        "source_url": source_url,
        "raw_document_type": filing.get("raw_document_type") or "",
        # text field max 30 chars on live schema
        "ingested_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "parse_status": _map_parse_status(filing.get("parse_status")),
        "member_name": filing.get("member_name") or "",
        "member_state": filing.get("member_state") or "",
    }
    # Empty member fields must be absent. A blank string on update clears
    # an id or party already stored from an earlier match.
    member_id = str(filing.get("member_id") or "").strip()
    if member_id:
        body["member_id"] = member_id
    member_party = str(filing.get("member_party") or "").strip()
    if member_party:
        body["member_party"] = member_party
    # drop Nones
    body = {k: v for k, v in body.items() if v is not None}

    existing = conn.find_one(
        "disclosure_filings",
        f'source_url = "{_escape_filter(source_url)}"',
    )
    if existing:
        updated = conn.update("disclosure_filings", existing["id"], body)
        return updated["id"]
    created = conn.create("disclosure_filings", body)
    return created["id"]


def upsert_transaction(conn: PocketBase, txn: dict) -> None:
    """Insert a transaction once per filing line.

    The same asset, date, and amount can occur twice in one PTR. Those are
    different trades. Identity is filing_id plus line_index (the transaction's
    0-based position in the filing), not the trade description.
    """
    filing_id = txn.get("filing_id")
    if not filing_id:
        raise ValueError("filing_id required")
    if skip_reason(txn, kind="transaction", require_name=False):
        return
    if txn.get("line_index") is None:
        raise ValueError("line_index required")
    try:
        line_index = int(txn["line_index"])
    except (TypeError, ValueError) as exc:
        raise ValueError("line_index must be an integer") from exc

    asset = txn.get("asset_name") or ""
    tdate = txn.get("transaction_date") or ""
    amount = txn.get("amount_range_raw") or ""
    ticker = (txn.get("ticker") or "").strip()

    filt = (
        f'filing_id = "{_escape_filter(str(filing_id))}"'
        f" && line_index = {line_index}"
    )
    existing = conn.find_one("transactions", filt)
    if existing:
        return

    body = {
        "filing_id": str(filing_id),
        "filer_relation": _map_filer_relation(txn.get("filer_relation")),
        "asset_name": asset,
        "ticker": ticker,
        "ticker_confidence": _map_ticker_conf(txn.get("ticker_confidence")),
        "transaction_type": _map_txn_type(txn.get("transaction_type")),
        "transaction_date": tdate,
        "notification_date": txn.get("notification_date") or "",
        "amount_range_raw": amount,
        "amount_range_low": txn.get("amount_range_low"),
        "amount_range_high": txn.get("amount_range_high"),
        "asset_category": txn.get("asset_category") or "",
        "line_index": line_index,
    }
    body = {k: v for k, v in body.items() if v is not None and v != ""}
    # required keys always kept, including line 0
    body["filing_id"] = str(filing_id)
    body["line_index"] = line_index
    body["ticker_confidence"] = _map_ticker_conf(txn.get("ticker_confidence"))
    if ticker:
        body["ticker"] = ticker
    member_id = str(txn.get("member_id") or "").strip()
    if member_id:
        body["member_id"] = member_id
    conn.create("transactions", body)


def upsert_annual_asset(conn: PocketBase, asset: dict) -> None:
    filing_id = asset.get("filing_id")
    name = asset.get("asset_name") or ""
    filt = (
        f'filing_id = "{_escape_filter(str(filing_id))}"'
        f' && asset_name = "{_escape_filter(name)}"'
    )
    existing = conn.find_one("annual_assets", filt)
    body = {
        "filing_id": str(filing_id),
        "member_id": asset.get("member_id") or "",
        "asset_name": name,
        "value_range_raw": asset.get("value_range_raw") or "",
        "value_range_low": asset.get("value_range_low"),
        "value_range_high": asset.get("value_range_high"),
        "income_type": asset.get("income_type") or "",
        "income_amount_range": asset.get("income_amount_range") or "",
    }
    body = {k: v for k, v in body.items() if v is not None and v != ""}
    body["filing_id"] = str(filing_id)
    if existing:
        conn.update("annual_assets", existing["id"], body)
    else:
        conn.create("annual_assets", body)


def log_ingestion_run(
    conn: PocketBase,
    source: str,
    found: int,
    parsed: int,
    failed: int,
    note: str = "",
    flagged: int = 0,
    duration_seconds: float | None = None,
) -> None:
    body = {
        "source": _map_source(source),
        "run_timestamp": datetime.now(timezone.utc).isoformat(),
        "filings_found": found,
        "filings_parsed_success": parsed,
        "filings_failed": failed,
        "filings_flagged_for_review": flagged,
        "error_message": note or "",
    }
    if duration_seconds is not None:
        body["duration_seconds"] = duration_seconds
    conn.create("ingestion_log", body)
    logger.info(
        "Logged ingestion run: source=%s found=%d parsed=%d failed=%d",
        source, found, parsed, failed,
    )


def find_member_id(
    conn: PocketBase,
    name: str,
    state: str | None = None,
    district: int | None = None,
    chamber: str | None = None,
    office: str | None = None,
    state_district: str | None = None,
) -> str | None:
    """Match a filer name to members.name / bioguide_id. Returns record id (text)."""
    if not name or not str(name).strip():
        return None

    if state_district and (state is None or district is None):
        sc, dist = parse_state_district(state_district)
        state = state or sc
        if district is None:
            district = dist

    state_code = normalize_state(state)
    parsed = parse_filer_name(name, office=office)
    if not parsed.full and not parsed.last:
        return None

    candidates: list[dict] = []

    # 1) Exact name variants
    for variant in parsed.variants:
        if not variant or " " not in variant and "," not in variant:
            continue
        # members.name is the display field (not full_name)
        rec = conn.find_one("members", f'name ~ "{_escape_filter(variant)}"')
        # Prefer exact case-insensitive — PB ~ is like/contains depending on version.
        # Also try equality:
        rec_eq = conn.find_one(
            "members", f'name = "{_escape_filter(variant.title())}"'
        ) or conn.find_one("members", f'name = "{_escape_filter(variant)}"')
        for r in (rec_eq, rec):
            if r and r not in candidates:
                # verify exact ignore-case if we used contains
                if r.get("name", "").lower() == variant.lower() or variant.lower() in (
                    r.get("name") or ""
                ).lower():
                    candidates.append(r)

    # 2) last-name filter + first-prefix in Python
    if parsed.last:
        data = conn.list(
            "members",
            filter=f'name ~ "{_escape_filter(parsed.last)}"',
            per_page=50,
        )
        for r in data.get("items") or []:
            mname = (r.get("name") or "").lower()
            tokens = mname.split()
            if not tokens:
                continue
            last_ok = tokens[-1] == parsed.last.lower() or parsed.last.lower() in mname
            first_ok = tokens[0].startswith(parsed.first.lower()[: max(3, len(parsed.first))]) or parsed.first.lower().startswith(tokens[0][:3])
            if last_ok and first_ok and r not in candidates:
                candidates.append(r)

    if not candidates:
        logger.info(
            "No member match for name=%r state=%s district=%s chamber=%s",
            name, state_code, district, chamber,
        )
        return None

    # Dedupe by id
    by_id = {c["id"]: c for c in candidates}
    if len(by_id) == 1:
        mid = next(iter(by_id))
        logger.info(
            "Member match: %r → id=%s (%s)",
            name, mid, by_id[mid].get("name"),
        )
        return mid

    # Disambiguate with state / district / chamber
    scored: list[tuple[int, str, str]] = []
    for mid, rec in by_id.items():
        score = 0
        m_state = normalize_state(rec.get("state"))
        if state_code and m_state:
            score += 10 if m_state == state_code else -20
        if district is not None and rec.get("district") is not None:
            try:
                score += 5 if int(rec["district"]) == int(district) else -10
            except (TypeError, ValueError):
                pass
        if chamber and rec.get("chamber"):
            score += 3 if str(rec["chamber"]).lower() == chamber.lower() else -5
        scored.append((score, mid, rec.get("name") or mid))

    scored.sort(key=lambda x: x[0], reverse=True)
    best_score, best_id, best_label = scored[0]
    ties = [s for s in scored if s[0] == best_score]
    if best_score > 0 and len(ties) == 1:
        logger.info(
            "Member match disambiguated (score=%d): %r → id=%s (%s)",
            best_score, name, best_id, best_label,
        )
        return best_id

    logger.warning(
        "Ambiguous member match for %r — candidates=%s; skipping",
        name,
        [(s[1], s[2], s[0]) for s in scored[:5]],
    )
    return None


def get_member(conn: PocketBase, member_id: str) -> dict | None:
    data = conn.list("members", filter=f'id = "{_escape_filter(member_id)}"', per_page=1)
    items = data.get("items") or []
    if items:
        return items[0]
    # try bioguide
    return conn.find_one("members", f'bioguide_id = "{_escape_filter(member_id)}"')
