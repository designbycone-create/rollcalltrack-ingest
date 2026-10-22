"""Fill member_id and party on disclosure rows already stored in Horizons.

Lists filings and transactions that are missing a member id or a party,
resolves them (full name + full state name, then last name + state, then
bioguide), and re-posts through ``POST /hcgi/api/ingest/disclosures``.

The write replaces a filing's transactions, so this command sends back
every transaction it listed for that filing. It refuses to post when the
transaction list cannot be loaded.

Dry-run is the default and prints every planned change. Nothing is written
unless ``--live`` is passed.

    python -m ingest.backfill_members
    python -m ingest.backfill_members --live
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

from . import config
from .ingest_client import HorizonsIngestClient, get_client
from .names import state_display_name

logger = logging.getLogger("rollcalltrack_ingest.backfill_members")

_FILING_KEYS = (
    "chamber",
    "filing_type",
    "filing_date",
    "source_url",
    "raw_document_type",
    "parse_status",
    "parse_failure_reason",
    "member_name",
    "member_state",
    "member_id",
    "member_party",
    "bioguide_id",
)


def _text(value) -> str:
    return str(value if value is not None else "").strip()


def _party(record: dict) -> str:
    return _text(record.get("member_party") or record.get("party"))


def _needs_link(record: dict) -> bool:
    return not _text(record.get("member_id")) or not _party(record)


def _filing_record_id(filing: dict) -> str:
    return _text(filing.get("id"))


def _lookup_state(record: dict) -> str:
    raw = _text(record.get("member_state") or record.get("state"))
    return state_display_name(raw) or raw


def _public_filing(record: dict) -> dict:
    out = {}
    for key in _FILING_KEYS:
        value = record.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            if not value.strip():
                continue
            out[key] = value.strip()
        elif value != "":
            out[key] = value
    return out


def _nested_transactions(filings: list[dict]) -> list[dict]:
    rows = []
    for filing in filings:
        if "transactions" not in filing:
            continue
        parent = _filing_record_id(filing)
        for txn in filing.get("transactions") or []:
            if not isinstance(txn, dict):
                continue
            row = dict(txn)
            if parent and not _text(row.get("filing_id")):
                row["filing_id"] = parent
            rows.append(row)
    return rows


def _merge_transactions(*groups: list[dict]) -> list[dict]:
    merged: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for group in groups:
        for txn in group:
            key = (_text(txn.get("filing_id")), _text(txn.get("line_index")))
            if key in seen and key != ("", ""):
                continue
            seen.add(key)
            merged.append(txn)
    return merged


def plan_member_updates(
    filings: list[dict],
    transactions: list[dict],
    resolver,
) -> list[dict]:
    """Return filing payloads that would gain a member id or party.

    ``resolver(name, state, bioguide_id, chamber, office)`` returns a
    member dict or None. Existing non-blank fields are left alone, and a
    miss does not clear them.
    """
    by_filing: dict[str, list[dict]] = {}
    for txn in transactions:
        by_filing.setdefault(_text(txn.get("filing_id")), []).append(txn)

    planned: list[dict] = []
    for filing in filings:
        filing_id = _filing_record_id(filing)
        if not _text(filing.get("source_url")):
            logger.info(
                "backfill skip filing id=%s: no source_url, cannot update",
                filing_id,
            )
            continue
        owned = list(by_filing.get(filing_id, []))
        if not _needs_link(filing) and not any(_needs_link(txn) for txn in owned):
            continue

        state = _lookup_state(filing)
        member = resolver(
            name=_text(filing.get("member_name") or filing.get("name")),
            state=state,
            bioguide_id=_text(filing.get("bioguide_id") or filing.get("bioguide"))
            or None,
            chamber=_text(filing.get("chamber")) or None,
            office=_text(filing.get("office")) or None,
        )
        updated = dict(filing)
        changes: list[dict] = []
        _apply_member(updated, member, changes, kind="filing", filing_id=filing_id)

        filing_member = _text(updated.get("member_id"))
        filing_party = _party(updated)
        txn_rows = []
        for txn in owned:
            row = dict(txn)
            _fill(
                row,
                "member_id",
                filing_member,
                changes,
                kind="transaction",
                filing_id=filing_id,
                source_url=_text(filing.get("source_url")),
                line_index=row.get("line_index"),
            )
            _fill(
                row,
                "member_party",
                filing_party,
                changes,
                kind="transaction",
                filing_id=filing_id,
                source_url=_text(filing.get("source_url")),
                line_index=row.get("line_index"),
            )
            txn_rows.append(row)
        _upgrade_state_code(updated, changes, filing_id=filing_id)

        if not changes:
            logger.info(
                "backfill unresolved source_url=%s name=%r state=%r bioguide=%r",
                filing.get("source_url"),
                filing.get("member_name"),
                filing.get("member_state"),
                filing.get("bioguide_id"),
            )
            continue
        planned.append(
            {
                "filing": _public_filing(updated),
                "transactions": txn_rows,
                "changes": changes,
            }
        )
    known = {_filing_record_id(filing) for filing in filings}
    for txn in transactions:
        filing_id = _text(txn.get("filing_id"))
        if filing_id in known or not _needs_link(txn):
            continue
        logger.info(
            "backfill skip transaction filing_id=%s line_index=%s: "
            "parent filing was not listed",
            filing_id,
            txn.get("line_index"),
        )
    return planned


def _apply_member(
    record: dict,
    member: dict | None,
    changes: list[dict],
    *,
    kind: str,
    filing_id: str,
) -> None:
    if not member:
        return
    source_url = _text(record.get("source_url"))
    resolved_id = _text(member.get("id") or member.get("member_id"))
    # A bioguide used as the row id is what made the writer 500 and drop the link.
    bioguide = _text(member.get("bioguide_id") or member.get("bioguide"))
    if resolved_id and bioguide and resolved_id.upper() == bioguide.upper():
        resolved_id = _text(member.get("member_id"))
        if resolved_id.upper() == bioguide.upper():
            resolved_id = ""
    _fill(
        record,
        "member_id",
        resolved_id,
        changes,
        kind=kind,
        filing_id=filing_id,
        source_url=source_url,
    )
    _fill(
        record,
        "member_party",
        _text(member.get("party") or member.get("member_party")),
        changes,
        kind=kind,
        filing_id=filing_id,
        source_url=source_url,
    )
    _fill(
        record,
        "bioguide_id",
        bioguide,
        changes,
        kind=kind,
        filing_id=filing_id,
        source_url=source_url,
    )
    display = state_display_name(member.get("state")) or _text(member.get("state"))
    if display and not _text(record.get("member_state")):
        _fill(
            record,
            "member_state",
            display,
            changes,
            kind=kind,
            filing_id=filing_id,
            source_url=source_url,
        )


def _upgrade_state_code(record: dict, changes: list[dict], *, filing_id: str) -> None:
    """Replace a stored USPS code with the full state name when we are already updating."""
    if not changes:
        return
    current = _text(record.get("member_state"))
    display = state_display_name(current)
    if not display or len(current) != 2 or current == display:
        return
    record["member_state"] = display
    changes.append(
        {
            "kind": "filing",
            "field": "member_state",
            "before": current,
            "after": display,
            "filing_id": filing_id,
            "source_url": _text(record.get("source_url")),
            "line_index": "",
        }
    )


def _fill(
    record: dict,
    key: str,
    value: str,
    changes: list[dict],
    *,
    kind: str,
    filing_id: str,
    source_url: str,
    line_index=None,
) -> None:
    current = _text(record.get(key))
    new = _text(value)
    if current or not new:
        return
    record[key] = new
    changes.append(
        {
            "kind": kind,
            "field": key,
            "before": current,
            "after": new,
            "filing_id": filing_id,
            "source_url": source_url or _text(record.get("source_url")),
            "line_index": "" if line_index is None else line_index,
        }
    )


def _format_change(change: dict) -> str:
    line = change.get("line_index")
    where = f" line_index={line}" if line != "" and line is not None else ""
    return (
        f"backfill {change['kind']} source_url={change.get('source_url', '')} "
        f"filing_id={change.get('filing_id', '')}{where} "
        f"{change['field']}: {change['before']!r} -> {change['after']!r}"
    )


def run(client: HorizonsIngestClient | None = None, live: bool = False) -> dict:
    """Print planned member links. POST them only when ``live`` is true."""
    hz = client if client is not None else get_client()
    if hz is None or not hz.configured:
        logger.error("HORIZONS_BASE_URL and INGEST_API_KEY are required")
        return {"planned": 0, "updated": 0, "failed": 1, "error": "INGEST_API_KEY required"}

    filings = hz.list_disclosure_filings()
    try:
        listed = hz.list_disclosure_transactions()
    except RuntimeError as exc:
        if not any(isinstance(row, dict) and "transactions" in row for row in filings):
            logger.error(
                "backfill refused: %s. The disclosures write replaces child "
                "transactions, so they have to be listed before an update.",
                exc,
            )
            return {"planned": 0, "updated": 0, "failed": 1, "error": "transactions unlistable"}
        logger.warning("transaction list unavailable (%s); using rows nested on filings", exc)
        listed = []
    transactions = _merge_transactions(listed, _nested_transactions(filings))

    def resolver(name, state, bioguide_id=None, chamber=None, office=None):
        return hz.lookup_member_by_name(
            name,
            state=state,
            bioguide_id=bioguide_id,
            chamber=chamber,
            office=office,
        )

    planned = plan_member_updates(filings, transactions, resolver)
    updated = 0
    failed = 0
    for item in planned:
        for change in item["changes"]:
            line = _format_change(change)
            logger.info(line)
            print(line)
        if not live:
            continue
        try:
            result = hz.submit_filing(
                item["filing"],
                transactions=item["transactions"],
                ignore_date=True,
            )
            if result.get("skipped"):
                failed += 1
                logger.error(
                    "backfill update skipped source_url=%s",
                    item["filing"].get("source_url"),
                )
                continue
            updated += 1
            logger.info(
                "backfill updated source_url=%s changes=%d",
                item["filing"].get("source_url"),
                len(item["changes"]),
            )
        except Exception as exc:
            failed += 1
            logger.error(
                "backfill update failed source_url=%s: %s",
                item["filing"].get("source_url"),
                exc,
            )
    summary = (
        f"backfill planned={len(planned)} updated={updated if live else 0} "
        f"failed={failed} live={int(live)}"
    )
    logger.info(summary)
    print(summary)
    return {
        "planned": len(planned),
        "updated": updated if live else 0,
        "failed": failed,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill member_id and party on House/Senate PTR filings. "
            "Prints the plan. Posts only with --live."
        )
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="POST the planned updates. Without this flag the command only prints them.",
    )
    args = parser.parse_args(argv)
    if args.live:
        os.environ["INGEST_LIVE"] = "1"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        config.require_config(dry_run=False, source="house")
    except RuntimeError as exc:
        logger.error("%s", exc)
        return 1
    result = run(live=args.live)
    return 1 if result.get("failed") else 0


if __name__ == "__main__":
    sys.exit(main())
