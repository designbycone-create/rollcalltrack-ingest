"""Senate financial disclosure ingest — efdsearch.senate.gov scrape.

Live-verified (2026-08):
  1. GET /search/home/ → extract csrfmiddlewaretoken
  2. POST /search/home/ with prohibition_agreement=1 + csrf → session cookie
  3. POST /search/report/data/ (DataTables JSON) with report_types='[11]' for PTR
  4. GET detail HTML at /search/view/ptr/{uuid}/ and parse table.table-striped

Report type codes (form checkboxes):
  7=Annual, 11=Periodic Transactions, 10=Due Date Extension,
  14=Blind Trusts, 15=Other Documents
"""
from __future__ import annotations

import logging
import re
import time
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

from . import config, db

logger = logging.getLogger("rollcalltrack_ingest.senate")


def _new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": config.USER_AGENT})
    return s


def _accept_agreement(session: requests.Session) -> str:
    """Accept the eFD prohibited-use agreement. Returns CSRF token for later POSTs."""
    resp = session.get(
        config.SENATE_EFD_SEARCH_HOME_URL,
        timeout=config.REQUEST_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")

    csrf_input = soup.find("input", {"name": "csrfmiddlewaretoken"})
    if csrf_input is None or not csrf_input.get("value"):
        raise RuntimeError(
            "Senate eFD home page missing csrfmiddlewaretoken — form may have changed"
        )
    csrf_token = csrf_input["value"]

    # Live form: checkbox name=prohibition_agreement value=1, id=agree_statement
    payload = {
        "csrfmiddlewaretoken": csrf_token,
        "prohibition_agreement": "1",
    }
    resp = session.post(
        config.SENATE_EFD_SEARCH_HOME_URL,
        data=payload,
        timeout=config.REQUEST_TIMEOUT_SECONDS,
        headers={"Referer": config.SENATE_EFD_SEARCH_HOME_URL},
    )
    resp.raise_for_status()
    if "/search" not in resp.url and "filedReports" not in resp.text:
        logger.warning(
            "Agreement POST did not clearly land on search page (url=%s); continuing",
            resp.url,
        )

    # Prefer cookie token for subsequent AJAX (matches browser X-CSRFToken behavior)
    token = session.cookies.get("csrftoken") or csrf_token
    return token


def search_recent_filings(
    session: requests.Session,
    since: date,
    csrf_token: str,
    until: date | None = None,
    report_types: list[str] | None = None,
) -> list[dict]:
    """Query eFD DataTables endpoint for filings in [since, until].

    Returns list of dicts:
      first_name, last_name, name, office, filing_type, filing_date, detail_url
    """
    until = until or date.today()
    report_types = report_types or [config.SENATE_REPORT_TYPE_PTR]

    # Date format verified via working open-source scrapers + live probe:
    # "MM/DD/YYYY HH:MM:SS" (time component required for reliable filtering).
    start_str = since.strftime("%m/%d/%Y") + " 00:00:00"
    end_str = until.strftime("%m/%d/%Y") + " 23:59:59"

    results: list[dict] = []
    start = 0
    page_len = 100

    while True:
        payload = {
            "start": str(start),
            "length": str(page_len),
            "report_types": str(report_types).replace("'", ""),  # '[11]'
            "filer_types": "[]",
            "submitted_start_date": start_str,
            "submitted_end_date": end_str,
            "candidate_state": "",
            "senator_state": "",
            "office_id": "",
            "first_name": "",
            "last_name": "",
            "csrfmiddlewaretoken": csrf_token,
        }
        # Ensure JSON-array encoding even if report_types already looks right
        payload["report_types"] = "[" + ",".join(str(x) for x in report_types) + "]"

        resp = session.post(
            config.SENATE_EFD_REPORT_DATA_URL,
            data=payload,
            timeout=config.REQUEST_TIMEOUT_SECONDS,
            headers={
                "Referer": config.SENATE_EFD_SEARCH_URL,
                "X-CSRFToken": csrf_token,
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        if resp.status_code == 503:
            logger.warning("Senate report data returned 503; backing off 10s and retrying once")
            time.sleep(10)
            resp = session.post(
                config.SENATE_EFD_REPORT_DATA_URL,
                data=payload,
                timeout=config.REQUEST_TIMEOUT_SECONDS,
                headers={
                    "Referer": config.SENATE_EFD_SEARCH_URL,
                    "X-CSRFToken": csrf_token,
                    "X-Requested-With": "XMLHttpRequest",
                },
            )
        resp.raise_for_status()
        try:
            data = resp.json()
        except ValueError as e:
            raise RuntimeError(
                f"Senate report data non-JSON (status={resp.status_code}): {resp.text[:300]}"
            ) from e

        rows = data.get("data") or []
        if not rows:
            break

        for row in rows:
            # Row shape (verified live):
            # [first_name, last_name, office, report_link_html, date_received]
            if len(row) < 5:
                logger.warning("Unexpected Senate row shape: %r", row)
                continue
            first, last, office, link_html, filing_date = (
                row[0],
                row[1],
                row[2],
                row[3],
                row[4],
            )
            detail_url = _extract_href(link_html)
            filing_type = _extract_link_text(link_html) or "Periodic Transaction Report"
            results.append(
                {
                    "first_name": first,
                    "last_name": last,
                    "name": f"{first} {last}".strip(),
                    "office": office,
                    "filing_type": filing_type,
                    "filing_date": filing_date,
                    "detail_url": detail_url,
                }
            )

        start += page_len
        total = data.get("recordsFiltered") or data.get("recordsTotal") or 0
        if start >= int(total) or len(rows) < page_len:
            break
        time.sleep(config.REQUEST_DELAY_SECONDS)

    return results


def _extract_href(html_fragment: str) -> str | None:
    soup = BeautifulSoup(html_fragment, "lxml")
    a = soup.find("a")
    if not a or not a.get("href"):
        return None
    href = a["href"]
    if href.startswith("http"):
        return href
    return f"{config.SENATE_EFD_BASE_URL}{href}"


def _extract_link_text(html_fragment: str) -> str | None:
    soup = BeautifulSoup(html_fragment, "lxml")
    a = soup.find("a")
    return a.get_text(strip=True) if a else None


def parse_filing_detail(session: requests.Session, url: str) -> dict:
    """Fetch and parse a single PTR detail page.

    Live table headers:
      # | Transaction Date | Owner | Ticker | Asset Name | Asset Type | Type | Amount | Comment
    Paper filings under /search/view/paper/ have no HTML table — returned empty.
    """
    resp = session.get(url, timeout=config.REQUEST_TIMEOUT_SECONDS)
    resp.raise_for_status()

    # Session expiry redirects back to agreement page
    if "/search/home" in resp.url:
        raise RuntimeError("Senate session expired mid-run (redirected to agreement page)")

    soup = BeautifulSoup(resp.text, "lxml")
    transactions = []

    # Prefer tbody rows from the striped transactions table
    table = soup.find("table", class_=re.compile(r"\btable\b"))
    if table is None:
        # Paper PDF view — no structured HTML
        if "/view/paper/" in url:
            logger.info("Paper filing (no HTML table): %s", url)
        return {"transactions": [], "raw_html": resp.text, "paper": "/view/paper/" in url}

    body_rows = table.find("tbody")
    rows = body_rows.find_all("tr") if body_rows else table.find_all("tr")[1:]

    for row in rows:
        cells = [c.get_text(" ", strip=True) for c in row.find_all("td")]
        if len(cells) < 8:
            continue
        # [#], date, owner, ticker, asset, asset_type, type, amount, [comment]
        txn_date = cells[1]
        owner = cells[2]
        ticker = cells[3]
        asset_name = cells[4]
        asset_type = cells[5]
        order_type = cells[6]
        amount = cells[7]

        if not asset_name or not amount:
            continue

        transactions.append(
            {
                "asset_name": asset_name,
                "ticker": None if ticker in ("", "--", "—") else ticker,
                "ticker_confidence": "filed" if ticker not in ("", "--", "—") else "unconfirmed",
                "transaction_type": _normalize_order_type(order_type),
                "transaction_date": txn_date,
                "notification_date": None,  # not present on Senate PTR HTML
                "amount_range_raw": amount,
                "filer_relation": _normalize_owner(owner),
                "asset_category": asset_type or None,
            }
        )

    return {"transactions": transactions, "raw_html": resp.text, "paper": False}


def _normalize_order_type(raw: str) -> str:
    s = (raw or "").strip().lower()
    if "partial" in s and "sale" in s:
        return "sale_partial"
    if s.startswith("sale") or s == "s":
        return "sale"
    if s.startswith("purchase") or s == "p":
        return "purchase"
    if s.startswith("exchange") or s == "e":
        return "exchange"
    return s or "unknown"


def _normalize_owner(raw: str) -> str:
    s = (raw or "").strip().lower()
    if s in ("", "--", "—", "self"):
        return "self"
    if "spouse" in s:
        return "spouse"
    if "joint" in s:
        return "joint"
    if "child" in s or "dependent" in s:
        return "dependent_child"
    return s


def run(
    conn,
    days_back: int = 2,
    dry_run: bool = False,
    max_filings: int | None = None,
) -> dict:
    """days_back is intentionally short for the daily scheduled job."""
    found, parsed, failed = 0, 0, 0
    since = date.today() - timedelta(days=days_back)

    session = _new_session()
    try:
        csrf = _accept_agreement(session)
        time.sleep(config.REQUEST_DELAY_SECONDS)
        filings = search_recent_filings(session, since, csrf_token=csrf)
    except Exception as e:
        logger.error("Failed to search Senate eFD: %s", e)
        if conn is not None and not dry_run:
            db.log_ingestion_run(
                conn, "senate", 0, 0, 0, note=f"search failed: {e}"
            )
        return {"found": 0, "parsed": 0, "failed": 0}

    if max_filings is not None:
        filings = filings[:max_filings]

    found = len(filings)
    logger.info("Senate filings to process: %d (since %s)", found, since.isoformat())

    for rec in filings:
        time.sleep(config.REQUEST_DELAY_SECONDS)
        try:
            if not rec.get("detail_url"):
                logger.warning("No detail URL for %s — skipping", rec.get("name"))
                failed += 1
                continue

            member_id = None
            if conn is not None and not dry_run:
                member_id = db.find_member_id(
                    conn,
                    rec["name"],
                    chamber="senate",
                    office=rec.get("office"),
                )
                if member_id is None:
                    logger.warning(
                        "No member match for filer name: %s (%s) — skipping",
                        rec["name"],
                        rec.get("office"),
                    )
                    failed += 1
                    continue

            detail = parse_filing_detail(session, rec["detail_url"])

            filing = {
                "member_id": member_id,
                "chamber": "senate",
                "filing_type": rec["filing_type"],
                "filing_date": rec["filing_date"],
                "source_url": rec["detail_url"],
                "raw_document_type": "html" if not detail.get("paper") else "pdf",
                "parse_status": (
                    "paper_unparsed"
                    if detail.get("paper")
                    else ("parsed" if detail["transactions"] else "empty")
                ),
            }

            if dry_run:
                logger.info(
                    "[dry-run] Senate filing name=%r office=%r type=%s date=%s "
                    "url=%s transactions=%d paper=%s",
                    rec["name"],
                    rec.get("office"),
                    rec["filing_type"],
                    rec["filing_date"],
                    rec["detail_url"],
                    len(detail["transactions"]),
                    detail.get("paper"),
                )
                for t in detail["transactions"][:10]:
                    logger.info("[dry-run]   txn %s", t)
                if len(detail["transactions"]) > 10:
                    logger.info(
                        "[dry-run]   ... +%d more",
                        len(detail["transactions"]) - 10,
                    )
                parsed += 1
                continue

            filing_id = db.upsert_filing(conn, filing)
            for txn in detail["transactions"]:
                txn.update(
                    {
                        "filing_id": filing_id,
                        "member_id": member_id,
                        "amount_range_low": None,
                        "amount_range_high": None,
                    }
                )
                if "ticker" not in txn:
                    txn["ticker"] = None
                if "ticker_confidence" not in txn:
                    txn["ticker_confidence"] = "unconfirmed"
                if "asset_category" not in txn:
                    txn["asset_category"] = None
                db.upsert_transaction(conn, txn)

            parsed += 1
        except Exception as e:
            logger.error(
                "Failed to process Senate filing %s: %s",
                rec.get("detail_url"),
                e,
            )
            failed += 1

    if conn is not None and not dry_run:
        db.log_ingestion_run(conn, "senate", found, parsed, failed)

    return {"found": found, "parsed": parsed, "failed": failed}
