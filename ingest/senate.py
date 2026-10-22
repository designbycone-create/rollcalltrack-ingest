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

from . import config
from .ingest_client import HorizonsIngestClient, get_client
from .names import state_display_name, state_from_office

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
                "ticker_confidence": "high" if ticker not in ("", "--", "—") else "none",
                "transaction_type": _normalize_order_type(order_type),
                "transaction_date": txn_date,
                "notification_date": None,  # not present on Senate PTR HTML
                "amount_range_raw": amount,
                "filer_relation": _normalize_owner(owner),
                "asset_category": asset_type or None,
                "line_index": len(transactions),
            }
        )

    return {"transactions": transactions, "raw_html": resp.text, "paper": False}


def _normalize_order_type(raw: str) -> str:
    s = (raw or "").strip().lower()
    if "partial" in s and "sale" in s:
        return "sale"  # Horizons enum: purchase|sale|exchange|other
    if s.startswith("sale") or s == "s":
        return "sale"
    if s.startswith("purchase") or s == "p":
        return "purchase"
    if s.startswith("exchange") or s == "e":
        return "exchange"
    return "other"


def _normalize_owner(raw: str) -> str:
    s = (raw or "").strip().lower()
    if s in ("", "--", "—", "self"):
        return "self"
    if "spouse" in s:
        return "spouse"
    if "joint" in s:
        return "joint"
    if "child" in s or "dependent" in s:
        return "dependent"  # Horizons enum
    return "self"


def run(
    conn=None,
    days_back: int = 90,
    dry_run: bool = False,
    max_filings: int | None = None,
    client: HorizonsIngestClient | None = None,
) -> dict:
    """days_back defaults to 90 so filings stored without a member are rematched.

    conn is ignored. Direct PocketBase writes have been removed.
    """
    found, parsed, failed = 0, 0, 0
    since = date.today() - timedelta(days=days_back)
    hz = client if client is not None else (
        get_client() if config.use_horizons_api() and not dry_run else None
    )
    use_hz = hz is not None and hz.configured and not dry_run
    if not dry_run and not use_hz:
        logger.error("HORIZONS_BASE_URL and INGEST_API_KEY are required")
        return {
            "found": 0,
            "parsed": 0,
            "failed": 1,
            "error": "INGEST_API_KEY required",
        }

    session = _new_session()
    try:
        csrf = _accept_agreement(session)
        time.sleep(config.REQUEST_DELAY_SECONDS)
        filings = search_recent_filings(session, since, csrf_token=csrf)
    except Exception as e:
        logger.error("Failed to search Senate eFD: %s", e)
        if use_hz:
            hz.submit_ingestion_log(
                "senate_efd", 0, 0, 1, note=f"search failed: {e}"
            )
        return {"found": 0, "parsed": 0, "failed": 1, "error": "search failed"}

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

            detail = parse_filing_detail(session, rec["detail_url"])
            parse_status = (
                "failed"
                if detail.get("paper") or not detail["transactions"]
                else "parsed"
            )

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

            if use_hz:
                state_code = state_from_office(rec.get("office"))
                member = hz.lookup_member_by_name(
                    rec["name"],
                    state=state_code,
                    office=rec.get("office"),
                    chamber="senate",
                )
                if not member:
                    logger.warning(
                        "No Horizons member match for filer name=%r office=%r — "
                        "submitting without member_id",
                        rec["name"],
                        rec.get("office"),
                    )
                filing = {
                    "chamber": "senate",
                    "filing_type": "ptr",
                    "filing_date": rec["filing_date"],
                    "source_url": rec["detail_url"],
                    "raw_document_type": "html" if not detail.get("paper") else "pdf",
                    "parse_status": parse_status,
                    "member_name": (member or {}).get("name") or rec["name"],
                    "member_party": (member or {}).get("party") or "",
                    "member_state": (member or {}).get("state")
                    or state_display_name(state_code)
                    or "",
                }
                if member and member.get("id"):
                    filing["member_id"] = member["id"]
                bioguide = (member or {}).get("bioguide_id") or ""
                if bioguide:
                    filing["bioguide_id"] = bioguide
                result = hz.submit_filing(
                    filing, transactions=detail["transactions"]
                )
                if result.get("skipped"):
                    continue
                logger.info(
                    "Senate submitted filing_id=%s txns=%s name=%r",
                    result.get("filing_id"),
                    result.get("transactions_written"),
                    rec["name"],
                )
                parsed += 1
                continue

            logger.error(
                "Senate filing %s not submitted: INGEST_API_KEY is required",
                rec.get("detail_url"),
            )
            failed += 1
        except Exception as e:
            logger.error(
                "Failed to process Senate filing %s: %s",
                rec.get("detail_url"),
                e,
            )
            failed += 1

    if use_hz:
        hz.submit_ingestion_log("senate_efd", found, parsed, failed)

    return {"found": found, "parsed": parsed, "failed": failed}
