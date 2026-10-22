"""Fill member_id and party on disclosure rows already stored in Horizons.

Lists filings and transactions that are missing a member id or a party,
resolves them (full name + full state, then last name + state, then
bioguide), and writes through the existing disclosures ingest endpoint.

Dry run is the default. It prints every planned change and does not POST.
Pass ``--live`` to apply. Existing non-empty fields are left as they are.

    python -m ingest.backfill_members
    python -m ingest.backfill_members --live
"""
from __future__ import annotations

import argparse
import logging
import os
from collections import defaultdict

from .ingest_client import get_client

logger = logging.getLogger("rollcalltrack_ingest.backfill_members")

_MEMBER_KEYS = ("member_id", "member_party", "bioguide_id")


def _blank(value) -> bool:
    return not str(value or "").strip()


def _member_id(row: dict) -> str:
    return str(row.get("member_id") or "").strip()


def _party(row: dict) -> str:
    return str(row.get("member_party") or row.get("party") or "").strip()


def _bioguide(row: dict) -> str:
    return str(row.get("bioguide_id") or "").strip()


def _needs_member(row: dict) -> bool:
    return _blank(_member_id(row)) or _blank(_party(row))


def _filing_key(filing: dict) -> str:
    return str(filing.get("id") or filing.get("filing_id") or "").strip()


def _emit(line: str) -> None:
    logger.info(line)
    print(line)


def _group_transactions(filings: list[dict], transactions: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    seen: set[tuple[str, str, str]] = set()

    def add(filing_id: str, txn: dict) -> None:
        key = (
            filing_id,
            str(txn.get("id") or ""),
            str(txn.get("line_index")),
        )
        if key in seen:
            return
        seen.add(key)
        grouped[filing_id].append(txn)

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
    member_id = _member_id(filing) or str(member.get("id") or member.get("member_id") or "").strip()
    party = _party(filing) or str(member.get("party") or member.get("member_party") or "").strip()
    bioguide = _bioguide(filing) or str(member.get("bioguide_id") or "").strip()
    return member_id, party, bioguide


def _filing_updates(
    filing: dict,
    member_id: str,
    party: str,
    bioguide: str,
    resolved: dict | None,
) -> dict:
    updates = {}
    if _blank(_member_id(filing)) and member_id:
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


def _without_blanks(row: dict, keys: tuple[str, ...]) -> dict:
    cleaned = dict(row)
    for key in keys:
        if _blank(cleaned.get(key)):
            cleaned.pop(key, None)
    return cleaned


def run(client, live: bool = False) -> dict:
    """Plan member links. POST them only when ``live`` is true."""
    if live:
        os.environ["INGEST_LIVE"] = "1"

    filings = list(client.list_disclosure_filings() or [])
    transactions = list(client.list_disclosure_transactions() or [])
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
        if _blank(_member_id(filing)) or _blank(_party(filing)):
            resolved = client.lookup_member_by_name(
                filing.get("member_name") or filing.get("name") or "",
                state=filing.get("member_state") or filing.get("state"),
                bioguide_id=_bioguide(filing) or None,
                chamber=filing.get("chamber"),
                office=filing.get("office"),
                state_district=filing.get("state_district"),
            )
        member_id, party, bioguide = _resolved_values(filing, resolved)
        filing_updates = _filing_updates(filing, member_id, party, bioguide, resolved)
        txn_updates = []
        for txn in rows:
            updates = {}
            if _blank(_member_id(txn)) and member_id:
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

        payload = _without_blanks({**filing, **filing_updates}, _MEMBER_KEYS)
        payload_txns = []
        for txn, updates in zip(rows, txn_updates):
            payload_txns.append(
                _without_blanks({**txn, **updates}, ("member_id", "member_party"))
            )
        if not payload_txns:
            _emit(
                f"backfill filing {filing_id} lists no transactions; "
                "the write replaces child rows with this list"
            )
        if not live:
            continue
        client.submit_filing(
            payload,
            transactions=payload_txns,
            ignore_stale_date=True,
        )

    summary = {
        "dry_run": not live,
        "filings_seen": len(filings),
        "transactions_seen": len(transactions),
        "filings_changed": filings_changed,
        "transactions_changed": transactions_changed,
        "unresolved": unresolved,
    }
    _emit(
        "backfill "
        f"dry_run={summary['dry_run']} filings_changed={filings_changed} "
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
    args = parser.parse_args(argv)
    if args.live:
        os.environ["INGEST_LIVE"] = "1"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    result = run(client=get_client(), live=args.live)
    print(
        "backfill "
        f"dry_run={result['dry_run']} "
        f"filings_changed={result['filings_changed']} "
        f"transactions_changed={result['transactions_changed']} "
        f"unresolved={result['unresolved']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
