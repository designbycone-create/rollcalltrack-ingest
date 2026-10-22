"""Fill member_id and party on disclosure rows already stored in Horizons.

Lists filings and transactions that are missing a member id or a party,
resolves them (full name + full state, then last name + state, then
bioguide), and writes through the existing disclosures ingest endpoint.

The disclosures write replaces a filing's transactions. A live update
therefore sends back every transaction listed for that filing, including
``line_index``, ``ticker``, and ``ticker_confidence``. It does not POST
when that list is empty. A bioguide is never sent as ``member_id`` (that
value makes the writer return HTTP 500, and the retry stores the filing
with no member link).

Filings are read from ``GET /hcgi/api/members`` and
``GET /hcgi/api/members/{id}/disclosures``. That catalog cannot see a
filing whose member_id is blank. ``--relink-filing-ids`` loads those
Horizons ids from a single-filing GET. No working single-filing GET was
found: ingest paths return ``{"error":"Route not found"}``, and the
public id paths were Cloudflare 403 from the probe network. The command
exits non-zero and names the routes it tried. If the member catalog does
not answer, the command exits non-zero instead of reporting zero changes.

Dry run is the default. It prints every planned change and does not POST.
Pass ``--live`` to apply. Existing non-empty fields are left as they are.

    python -m ingest.backfill_members
    python -m ingest.backfill_members --live
"""
from __future__ import annotations

import argparse
import logging
import os
import re
from collections import defaultdict

from .ingest_client import DisclosureListError, get_client

logger = logging.getLogger("rollcalltrack_ingest.backfill_members")

_MEMBER_KEYS = ("member_id", "member_party", "bioguide_id")
_BIOGUIDE_ID = re.compile(r"^[A-Z]\d{6}$")
_FILL_IF_BLANK = (
    "ticker",
    "ticker_confidence",
    "asset_name",
    "transaction_type",
    "transaction_date",
    "amount_range_raw",
    "notification_date",
    "asset_category",
    "filer_relation",
    "member_id",
    "member_party",
)


def _blank(value) -> bool:
    return not str(value or "").strip()


def _member_id(row: dict) -> str:
    return str(row.get("member_id") or "").strip()


def _party(row: dict) -> str:
    return str(row.get("member_party") or row.get("party") or "").strip()


def _bioguide(row: dict) -> str:
    return str(row.get("bioguide_id") or "").strip()


def _is_bioguide_id(value, bioguide: str = "") -> bool:
    """True when this string is a congressional bioguide, not a Horizons id."""
    text = str(value or "").strip()
    if not text:
        return False
    if _BIOGUIDE_ID.fullmatch(text.upper()):
        return True
    known = str(bioguide or "").strip()
    return bool(known) and text.upper() == known.upper()


def _horizons_member_id(member: dict | None, bioguide: str = "") -> str:
    """PocketBase id from a lookup row. A bioguide is not accepted."""
    member = member or {}
    known = str(bioguide or member.get("bioguide_id") or member.get("bioguide") or "").strip()
    for key in ("id", "member_id"):
        value = str(member.get(key) or "").strip()
        if value and not _is_bioguide_id(value, known):
            return value
    return ""


def _usable_member_id(row: dict, bioguide: str = "") -> str:
    value = _member_id(row)
    if _is_bioguide_id(value, bioguide):
        return ""
    return value


def _needs_member(row: dict) -> bool:
    return _blank(_usable_member_id(row)) or _blank(_party(row))


def _filing_key(filing: dict) -> str:
    return str(filing.get("id") or filing.get("filing_id") or "").strip()


def _emit(line: str) -> None:
    logger.info(line)
    print(line)


def _fill_missing(dest: dict, src: dict) -> None:
    """Keep the first copy of a trade, and fill identity fields it lacks."""
    for field in _FILL_IF_BLANK:
        if _blank(dest.get(field)) and not _blank(src.get(field)):
            dest[field] = src[field]
    if dest.get("line_index") is None and src.get("line_index") is not None:
        dest["line_index"] = src["line_index"]
    for field in ("amount_range_low", "amount_range_high"):
        if dest.get(field) is None and src.get(field) is not None:
            dest[field] = src[field]


def _group_transactions(filings: list[dict], transactions: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    index: dict[tuple[str, str, str], dict] = {}

    def add(filing_id: str, txn: dict) -> None:
        row = dict(txn)
        key = (
            filing_id,
            str(row.get("id") or ""),
            str(row.get("line_index")),
        )
        existing = index.get(key)
        if existing is not None:
            _fill_missing(existing, row)
            return
        index[key] = row
        grouped[filing_id].append(row)

    for txn in transactions:
        add(str(txn.get("filing_id") or "").strip(), txn)
    for filing in filings:
        filing_id = _filing_key(filing)
        for txn in filing.get("transactions") or []:
            row = dict(txn)
            row.setdefault("filing_id", filing_id)
            add(filing_id, row)
    return grouped


def _resolved_values(filing: dict, resolved: dict | None) -> tuple[str, str, str]:
    member = resolved or {}
    bioguide = _bioguide(filing) or str(
        member.get("bioguide_id") or member.get("bioguide") or ""
    ).strip()
    member_id = _usable_member_id(filing, bioguide) or _horizons_member_id(member, bioguide)
    party = _party(filing) or str(member.get("party") or member.get("member_party") or "").strip()
    return member_id, party, bioguide


def _filing_updates(
    filing: dict,
    member_id: str,
    party: str,
    bioguide: str,
    resolved: dict | None,
) -> dict:
    updates = {}
    stored_id = _member_id(filing)
    if member_id and stored_id != member_id and (
        _blank(stored_id) or _is_bioguide_id(stored_id, bioguide)
    ):
        updates["member_id"] = member_id
    if _blank(_party(filing)) and party:
        updates["member_party"] = party
    if _blank(_bioguide(filing)) and bioguide:
        updates["bioguide_id"] = bioguide
    # Fill a blank name or state. Never replace one that is already stored.
    member = resolved or {}
    if _blank(filing.get("member_name")) and str(member.get("name") or "").strip():
        updates["member_name"] = str(member["name"]).strip()
    if _blank(filing.get("member_state")) and str(member.get("state") or "").strip():
        updates["member_state"] = str(member["state"]).strip()
    return updates


def _drop_bioguide_member_id(row: dict, bioguide: str) -> dict:
    cleaned = dict(row)
    if _is_bioguide_id(_member_id(cleaned), bioguide):
        cleaned.pop("member_id", None)
    return cleaned


def _without_blanks(row: dict, keys: tuple[str, ...]) -> dict:
    cleaned = dict(row)
    for key in keys:
        if _blank(cleaned.get(key)):
            cleaned.pop(key, None)
    return cleaned


def run(client, live: bool = False, relink_filing_ids: list[str] | None = None) -> dict:
    """Plan member links. POST them only when ``live`` is true.

    ``relink_filing_ids`` loads those Horizons ids directly. The member
    catalog cannot see a filing whose member_id is blank.
    """
    if live:
        os.environ["INGEST_LIVE"] = "1"

    relink_ids = [str(item).strip() for item in (relink_filing_ids or []) if str(item).strip()]
    try:
        if relink_ids:
            filings = []
            transactions = []
            for filing_id in relink_ids:
                filing = client.get_disclosure_filing(filing_id)
                filings.append(filing)
                transactions.extend(filing.get("transactions") or [])
        else:
            filings = list(client.list_disclosure_filings() or [])
            transactions = list(client.list_disclosure_transactions() or [])
    except DisclosureListError as exc:
        _emit(f"backfill failed: {exc}")
        return {
            "ok": False,
            "error": str(exc),
            "dry_run": not live,
            "filings_seen": 0,
            "transactions_seen": 0,
            "filings_changed": 0,
            "transactions_changed": 0,
            "unresolved": 0,
        }
    grouped = _group_transactions(filings, transactions)
    known_ids = {_filing_key(filing) for filing in filings}

    filings_changed = 0
    transactions_changed = 0
    unresolved = 0

    for filing_id, rows in grouped.items():
        if filing_id in known_ids:
            continue
        if any(_needs_member(txn) for txn in rows):
            unresolved += 1
            _emit(
                f"backfill unresolved transaction filing_id={filing_id or '?'} "
                "(filing not listed)"
            )

    for filing in filings:
        filing_id = _filing_key(filing)
        rows = grouped.get(filing_id, [])
        if not _needs_member(filing) and not any(_needs_member(txn) for txn in rows):
            continue
        if _blank(filing.get("source_url")):
            unresolved += 1
            _emit(f"backfill unresolved filing id={filing_id or '?'} missing source_url")
            continue

        resolved = None
        if _blank(_usable_member_id(filing)) or _blank(_party(filing)):
            bioguide_hint = _bioguide(filing)
            if not bioguide_hint and _is_bioguide_id(_member_id(filing)):
                bioguide_hint = _member_id(filing)
            resolved = client.lookup_member_by_name(
                filing.get("member_name") or filing.get("name") or "",
                state=filing.get("member_state") or filing.get("state"),
                bioguide_id=bioguide_hint or None,
                chamber=filing.get("chamber"),
                office=filing.get("office"),
                state_district=filing.get("state_district"),
            )
        member_id, party, bioguide = _resolved_values(filing, resolved)
        filing_updates = _filing_updates(filing, member_id, party, bioguide, resolved)
        txn_updates = []
        for txn in rows:
            updates = {}
            stored_id = _member_id(txn)
            if member_id and stored_id != member_id and (
                _blank(stored_id) or _is_bioguide_id(stored_id, bioguide)
            ):
                updates["member_id"] = member_id
            if _blank(_party(txn)) and party:
                updates["member_party"] = party
            txn_updates.append(updates)

        if not filing_updates and not any(txn_updates):
            unresolved += 1
            _emit(
                "backfill unresolved filing "
                f"id={filing_id or '?'} name={filing.get('member_name')!r} "
                f"state={filing.get('member_state')!r} "
                f"bioguide={filing.get('bioguide_id')!r}"
            )
            continue

        linked_bioguide = bioguide or _bioguide(filing)
        for field, new in filing_updates.items():
            _emit(
                f"backfill filing {filing_id} {field}: {filing.get(field)!r} -> {new!r}"
                f" bioguide={linked_bioguide}"
            )
        if filing_updates:
            filings_changed += 1
        for txn, updates in zip(rows, txn_updates):
            if not updates:
                continue
            transactions_changed += 1
            identity = f"filing={filing_id} line={txn.get('line_index')}"
            for field, new in updates.items():
                _emit(
                    f"backfill transaction {identity} {field}: {txn.get(field)!r} -> {new!r}"
                )

        if _is_bioguide_id(_member_id(filing), bioguide) and "member_id" not in filing_updates:
            _emit(
                f"backfill filing {filing_id} member_id: {_member_id(filing)!r} -> omitted"
                " bioguide is not a member_id"
            )
        payload = _drop_bioguide_member_id(
            _without_blanks({**filing, **filing_updates}, _MEMBER_KEYS),
            bioguide,
        )
        payload_txns = []
        for txn, updates in zip(rows, txn_updates):
            if _is_bioguide_id(_member_id(txn), bioguide) and "member_id" not in updates:
                _emit(
                    f"backfill transaction filing={filing_id} line={txn.get('line_index')} "
                    f"member_id: {_member_id(txn)!r} -> omitted bioguide is not a member_id"
                )
            payload_txns.append(
                _drop_bioguide_member_id(
                    _without_blanks({**txn, **updates}, ("member_id", "member_party")),
                    bioguide,
                )
            )
        if not payload_txns:
            unresolved += 1
            _emit(
                f"backfill filing {filing_id} not posted: no transactions listed; "
                "the write replaces child rows and would drop trades"
            )
            continue
        if not live:
            continue
        client.submit_filing(
            payload,
            transactions=payload_txns,
            ignore_stale_date=True,
        )

    summary = {
        "ok": True,
        "dry_run": not live,
        "filings_seen": len(filings),
        "transactions_seen": len(transactions),
        "filings_changed": filings_changed,
        "transactions_changed": transactions_changed,
        "unresolved": unresolved,
    }
    _emit(
        "backfill "
        f"dry_run={summary['dry_run']} "
        f"filings_seen={summary['filings_seen']} "
        f"transactions_seen={summary['transactions_seen']} "
        f"filings_changed={filings_changed} "
        f"transactions_changed={transactions_changed} unresolved={unresolved}"
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill member_id and party on House/Senate PTR filings. "
            "Prints the planned changes. Writes only with --live."
        )
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="POST the planned member updates. Omit this for a dry run.",
    )
    parser.add_argument(
        "--relink-filing-ids",
        default="",
        help=(
            "Comma-separated Horizons filing ids to fetch and relink. "
            "Skips the member catalog, which cannot see a filing with no member_id."
        ),
    )
    args = parser.parse_args(argv)
    if args.live:
        os.environ["INGEST_LIVE"] = "1"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    relink_ids = [part.strip() for part in args.relink_filing_ids.split(",") if part.strip()]
    kwargs = {"client": get_client(), "live": args.live}
    if relink_ids:
        kwargs["relink_filing_ids"] = relink_ids
    result = run(**kwargs)
    if not result.get("ok", True):
        print(result.get("error") or "backfill failed: disclosure list route did not answer")
        return 1
    print(
        "backfill "
        f"dry_run={result['dry_run']} "
        f"filings_seen={result.get('filings_seen', 0)} "
        f"transactions_seen={result.get('transactions_seen', 0)} "
        f"filings_changed={result['filings_changed']} "
        f"transactions_changed={result['transactions_changed']} "
        f"unresolved={result['unresolved']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
