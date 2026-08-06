"""House financial disclosure ingest — bulk ZIP feed + PTR PDF parsing.

Live-verified (2026-08) against disclosures-clerk.house.gov:
  - Bulk ZIP: /public_disc/financial-pdfs/{year}FD.zip
  - Index inside ZIP:  {year}FD.xml  (preferred) and fixed-width {year}FD.txt
  - PTR PDFs: /public_disc/ptr-pdfs/{year}/{DocID}.pdf
  - Other FD PDFs: /public_disc/financial-pdfs/{year}/{DocID}.pdf
  - FilingType "P" = Periodic Transaction Report
"""
from __future__ import annotations

import io
import logging
import re
import time
import zipfile
from datetime import date, datetime, timedelta
from xml.etree import ElementTree as ET

import pdfplumber
import requests

from . import config, db
from .ingest_client import HorizonsIngestClient, get_client

logger = logging.getLogger("rollcalltrack_ingest.house")

# Owner codes used on House PTR forms
_OWNER_MAP = {
    "SP": "spouse",
    "JT": "joint",
    "DC": "dependent_child",
    "C": "child",
}
_TYPE_MAP = {
    "P": "purchase",
    "S": "sale",
    "E": "exchange",
}

# Match a fully-normalized single-line PTR transaction row
_TXN_LINE_RE = re.compile(
    r"(?:(?P<owner>SP|JT|DC|C)\s+)?"
    r"(?P<asset>.+?)\s+"
    r"(?P<type>S\s*\(\s*partial\s*\)|[PSE])\s+"
    r"(?P<txn_date>\d{2}/\d{2}/\d{4})\s+"
    r"(?P<notify_date>\d{2}/\d{2}/\d{4})\s+"
    r"(?P<amount_range>\$[\d,]+\s*-\s*\$[\d,]+)"
)


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": config.USER_AGENT})
    return s


def download_bulk_index(year: int, session: requests.Session | None = None) -> list[dict]:
    """Download and parse the House Clerk's bulk annual index ZIP.

    Returns dicts:
      name, state_district, filing_type, filing_type_code, filing_date,
      year, doc_id, pdf_url
    """
    session = session or _session()
    url = config.HOUSE_BULK_ZIP_URL_TEMPLATE.format(year=year)
    logger.info("Fetching House bulk index: %s", url)
    resp = session.get(url, timeout=config.REQUEST_TIMEOUT_SECONDS)
    resp.raise_for_status()

    records: list[dict] = []
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        # Prefer XML — structured and unambiguous. Fall back to TXT if needed.
        xml_names = [n for n in zf.namelist() if n.lower().endswith(".xml")]
        txt_names = [n for n in zf.namelist() if n.lower().endswith(".txt")]
        if xml_names:
            raw = zf.read(xml_names[0])
            records = _parse_index_xml(raw)
        elif txt_names:
            raw = zf.read(txt_names[0]).decode("utf-8", errors="replace")
            records = _parse_index_txt(raw, year=year)
        else:
            logger.error("No XML/TXT index found in bulk ZIP; entries=%s", zf.namelist())
    logger.info("Parsed %d filing records from House bulk index", len(records))
    return records


def _pdf_url(filing_type_code: str, year: str | int, doc_id: str) -> str:
    year = str(year)
    doc_id = str(doc_id).strip()
    if filing_type_code == "P":
        return config.HOUSE_PTR_PDF_URL_TEMPLATE.format(year=year, doc_id=doc_id)
    return config.HOUSE_FD_PDF_URL_TEMPLATE.format(year=year, doc_id=doc_id)


def _format_name(prefix: str, first: str, last: str, suffix: str) -> str:
    parts = [p.strip() for p in (first, last) if p and p.strip()]
    name = " ".join(parts)
    # Keep Hon./Mr. out of the match key — members table stores plain names.
    return name


def _parse_index_xml(raw: bytes) -> list[dict]:
    """Parse {year}FD.xml Member elements."""
    root = ET.fromstring(raw)
    records = []
    for member in root.findall(".//Member"):
        doc_id = (member.findtext("DocID") or "").strip()
        if not doc_id:
            continue
        year = (member.findtext("Year") or "").strip()
        ftype = (member.findtext("FilingType") or "").strip()
        first = member.findtext("First") or ""
        last = member.findtext("Last") or ""
        prefix = member.findtext("Prefix") or ""
        suffix = member.findtext("Suffix") or ""
        state_dst = (member.findtext("StateDst") or "").strip()
        filing_date = (member.findtext("FilingDate") or "").strip()
        records.append(
            {
                "name": _format_name(prefix, first, last, suffix),
                "state_district": state_dst,
                "filing_type_code": ftype,
                "filing_type": config.HOUSE_FILING_TYPE_LABELS.get(ftype, ftype or "unknown"),
                "filing_date": filing_date,
                "year": year,
                "doc_id": doc_id,
                "pdf_url": _pdf_url(ftype, year, doc_id),
            }
        )
    return records


def _parse_index_txt(raw_text: str, year: int) -> list[dict]:
    """Fallback parser for the fixed-width / concatenated {year}FD.txt.

    The TXT is a header line followed by rows with no reliable delimiter —
    fields are jammed together (PrefixLastFirstSuffixFilingTypeStateDst...).
    Prefer XML. This fallback is best-effort via a trailing DocID + date pattern.
    """
    records = []
    # Trailing pattern: FilingType(1) StateDst(optional) Year FilingDate DocID
    # Example end: ...PAL0420259/10/202520032062
    row_re = re.compile(
        r"(?P<head>.*?)"
        r"(?P<ftype>[A-Z])"
        r"(?P<state>(?:[A-Z]{2}\d{2})?)"
        r"(?P<year>\d{4})"
        r"(?P<date>\d{1,2}/\d{1,2}/\d{4})"
        r"(?P<doc>\d+)\s*$"
    )
    for i, line in enumerate(raw_text.splitlines()):
        if i == 0 and "DocID" in line:
            continue
        m = row_re.search(line)
        if not m:
            continue
        ftype = m.group("ftype")
        doc_id = m.group("doc")
        y = m.group("year")
        # head is Prefix+Last+First+Suffix mashed — unusable for clean names;
        # XML should be preferred. Store raw head as name for traceability.
        head = m.group("head").strip()
        records.append(
            {
                "name": head,
                "state_district": m.group("state"),
                "filing_type_code": ftype,
                "filing_type": config.HOUSE_FILING_TYPE_LABELS.get(ftype, ftype),
                "filing_date": m.group("date"),
                "year": y,
                "doc_id": doc_id,
                "pdf_url": _pdf_url(ftype, y, doc_id),
            }
        )
    return records


def _parse_index_file(raw_text: str) -> list[dict]:
    """Back-compat entry used by tests; prefer download_bulk_index."""
    if raw_text.lstrip().startswith("<?xml") or raw_text.lstrip().startswith("<FinancialDisclosure"):
        return _parse_index_xml(raw_text.encode("utf-8"))
    return _parse_index_txt(raw_text, year=date.today().year)


def parse_pdf(pdf_url: str, session: requests.Session | None = None) -> dict:
    """Download a filing PDF and extract text.

    Returns {text, used_ocr: bool, page_count, empty_pages}.
    OCR fallback is intentionally not implemented yet: live sampling of
    recent PTR PDFs (2025) all had extractable text layers. Revisit if
    empty_pages becomes common on older/paper filings.
    """
    session = session or _session()
    resp = session.get(pdf_url, timeout=config.REQUEST_TIMEOUT_SECONDS)
    resp.raise_for_status()

    text_parts = []
    used_ocr = False
    empty_pages = 0
    page_count = 0
    with pdfplumber.open(io.BytesIO(resp.content)) as pdf:
        page_count = len(pdf.pages)
        for page in pdf.pages:
            page_text = page.extract_text() or ""
            if not page_text.strip():
                empty_pages += 1
                used_ocr = True
                # OCR TODO (pytesseract): only worth wiring if empty_pages is common.
                continue
            text_parts.append(page_text)

    return {
        "text": "\n".join(text_parts),
        "used_ocr": used_ocr and not any(text_parts),
        "page_count": page_count,
        "empty_pages": empty_pages,
    }


def _normalize_ptr_text(pdf_text: str) -> str:
    """Repair multi-line PTR rows so each transaction is one line.

    Real filings wrap amount ranges ("$15,001 -\\n$50,000") and sometimes
    split asset names ("Thermo Fisher... Common\\nStock (TMO)").
    """
    text = pdf_text.replace("\x00", "")
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        if re.search(r"\$[\d,]+\s*-\s*$", line):
            j = i + 1
            asset_extra: list[str] = []
            while j < len(lines):
                nxt = lines[j].strip()
                if not nxt:
                    j += 1
                    continue
                if re.match(r"^F\s+S:", nxt) or nxt.startswith("S O:") or nxt.startswith("Subholding"):
                    j += 1
                    continue
                if re.search(r"\d{2}/\d{2}/\d{4}", nxt):
                    break
                m = re.search(r"\$[\d,]+", nxt)
                if m:
                    before = nxt[: m.start()].strip()
                    if before and not re.match(r"^\[", before):
                        before = re.sub(r"\s*\[[A-Z]{1,4}\]\s*$", "", before).strip()
                        if before:
                            asset_extra.append(before)
                    line = f"{line} {m.group(0)}"
                    j += 1
                    break
                if re.match(r"^\(", nxt) or re.match(r"^\[[A-Z]", nxt):
                    asset_extra.append(
                        re.sub(r"\s*\[[A-Z]{1,4}\]\s*$", "", nxt).strip()
                    )
                    j += 1
                    continue
                if not re.match(
                    r"^(ID |Name:|Status:|Digitally|I CERTIFY|\*|Filing ID)", nxt
                ):
                    asset_extra.append(nxt)
                    j += 1
                    continue
                break
            if asset_extra:
                m2 = re.search(
                    r"^(.*?)(\s+(?:S\s*\(\s*partial\s*\)|[PSE])\s+"
                    r"\d{2}/\d{2}/\d{4}\s+\d{2}/\d{2}/\d{4}\s+\$)",
                    line,
                )
                if m2:
                    line = (
                        m2.group(1)
                        + " "
                        + " ".join(asset_extra)
                        + m2.group(2)
                        + line[m2.end() :]
                    )
            out.append(line)
            i = j
            continue

        stripped = line.strip()
        # Continuation after a complete transaction row: ticker residue or
        # the second half of a wrapped asset name ("Common Stock (CHRW) [ST]").
        if out and _TXN_LINE_RE.search(out[-1]) and not re.search(
            r"\d{2}/\d{2}/\d{4}", stripped
        ):
            is_ticker_only = bool(
                re.match(r"^\([^)]+\)\s*(?:\[[A-Z]{1,4}\])?\s*$", stripped)
            )
            is_name_cont = bool(
                re.match(
                    r"^(?:Common\s+Stock|Stock|plc|Inc\.?|Corp\.?|Class\s+[A-Z]|"
                    r"Manufacturing|Technologies|Holdings|Ordinary|American\s+Depositary|"
                    r"\([^)]+\)|\d)",
                    stripped,
                    re.I,
                )
            ) and not re.match(
                r"^(?:F\s+S:|S\s+O:|ID |Name:|Status:|Digitally|I CERTIFY|\*|Filing ID|Yes No)",
                stripped,
            )
            if is_ticker_only or is_name_cont:
                extra = re.sub(r"\s*\[[A-Z]{1,4}\]\s*$", "", stripped).strip()
                prev = out[-1]
                m2 = re.search(
                    r"^(.*?)(\s+(?:S\s*\(\s*partial\s*\)|[PSE])\s+\d{2}/\d{2}/\d{4})",
                    prev,
                )
                if m2 and extra and extra not in prev:
                    out[-1] = (
                        m2.group(1) + " " + extra + m2.group(2) + prev[m2.end() :]
                    )
                i += 1
                continue

        out.append(line)
        i += 1
    return "\n".join(out)


def parse_ptr_transactions(pdf_text: str) -> list[dict]:
    """Extract transaction rows from House PTR PDF text.

    Layout (verified on live 2025 PTRs):
      [Owner?] Asset Name [Type] TxnDate NotifyDate $low - $high
    Types: P / S / S (partial) / E. Amounts are disclosed ranges only.
    """
    text = _normalize_ptr_text(pdf_text)
    transactions = []
    for line in text.splitlines():
        m = _TXN_LINE_RE.search(line)
        if not m:
            continue
        asset = re.sub(r"\s+", " ", m.group("asset")).strip()
        if any(
            bad in asset
            for bad in (
                "CERTIFY",
                "Clerk of the House",
                "Digitally Signed",
                "Filing ID",
            )
        ):
            continue
        asset = re.sub(r"\bS O:\s*.*$", "", asset).strip()
        asset = re.sub(r"\bF S:\s*\w+", "", asset).strip()
        if not asset:
            continue

        ttype_raw = re.sub(r"\s+", " ", m.group("type")).strip()
        if "partial" in ttype_raw.lower():
            ttype = "sale_partial"
        else:
            ttype = _TYPE_MAP.get(ttype_raw[0].upper(), "unknown")

        owner_code = (m.group("owner") or "").upper()
        filer_relation = _OWNER_MAP.get(owner_code, "self")

        transactions.append(
            {
                "asset_name": asset,
                "filer_relation": filer_relation,
                "transaction_type": ttype,
                "transaction_date": m.group("txn_date"),
                "notification_date": m.group("notify_date"),
                "amount_range_raw": re.sub(
                    r"\s+", " ", m.group("amount_range")
                ).strip(),
            }
        )
    return transactions


def _parse_filing_date(s: str) -> date | None:
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except (ValueError, AttributeError):
            continue
    return None


def run(
    conn=None,
    year: int | None = None,
    dry_run: bool = False,
    days_back: int | None = 14,
    max_filings: int | None = None,
    ptr_only: bool = True,
    client: HorizonsIngestClient | None = None,
) -> dict:
    """Ingest House filings.

    days_back: only process filings whose FilingDate is within this many
      days of today. None = no date filter (full year — slow). Default 14
      so the daily job doesn't re-download every PTR of the year.
    max_filings: optional hard cap (useful for dry-run smoke tests).
    ptr_only: this pass only handles PTRs (FilingType P).
    client: Horizons HTTP client (preferred). Falls back to PocketBase conn.
    """
    year = year or date.today().year
    found, parsed, failed = 0, 0, 0
    session = _session()
    hz = client if client is not None else (
        get_client() if config.use_horizons_api() and not dry_run else None
    )
    use_hz = hz is not None and hz.configured and not dry_run

    try:
        index_records = download_bulk_index(year, session=session)
    except Exception as e:
        logger.error("Failed to fetch House bulk index: %s", e)
        if use_hz:
            hz.submit_ingestion_log(
                "house_clerk", 0, 0, 0, note=f"index fetch failed: {e}"
            )
        elif conn is not None and not dry_run:
            db.log_ingestion_run(
                conn, "house", 0, 0, 0, note=f"index fetch failed: {e}"
            )
        return {"found": 0, "parsed": 0, "failed": 0}

    if ptr_only:
        index_records = [r for r in index_records if r.get("filing_type_code") == "P"]

    if days_back is not None:
        cutoff = date.today() - timedelta(days=days_back)
        filtered = []
        for r in index_records:
            fd = _parse_filing_date(r.get("filing_date") or "")
            if fd is None or fd >= cutoff:
                filtered.append(r)
        logger.info(
            "House date filter days_back=%s: %d -> %d records",
            days_back,
            len(index_records),
            len(filtered),
        )
        index_records = filtered

    # Newest first so dry-run / capped runs hit recent filings
    def _sort_key(r):
        d = _parse_filing_date(r.get("filing_date") or "")
        return d or date.min

    index_records.sort(key=_sort_key, reverse=True)

    if max_filings is not None:
        index_records = index_records[:max_filings]

    found = len(index_records)
    logger.info("House filings to process: %d", found)

    for rec in index_records:
        time.sleep(config.REQUEST_DELAY_SECONDS)
        try:
            pdf_result = parse_pdf(rec["pdf_url"], session=session)
            txns = []
            if rec.get("filing_type_code") == "P" or "ptr" in (
                rec.get("filing_type") or ""
            ).lower():
                txns = parse_ptr_transactions(pdf_result["text"])

            pb_filing_type = config.HOUSE_TO_PB_FILING_TYPE.get(
                rec.get("filing_type_code") or "", "ptr"
            )
            state_code = (rec.get("state_district") or "")[:2] or None
            parse_status = (
                "failed"
                if pdf_result["used_ocr"] or (not txns and pb_filing_type == "ptr")
                else "parsed"
            )

            if dry_run:
                logger.info(
                    "[dry-run] House filing name=%r type=%s date=%s url=%s "
                    "pages=%s empty_pages=%s transactions=%d",
                    rec["name"],
                    rec["filing_type"],
                    rec["filing_date"],
                    rec["pdf_url"],
                    pdf_result["page_count"],
                    pdf_result["empty_pages"],
                    len(txns),
                )
                for t in txns[:10]:
                    logger.info("[dry-run]   txn %s", t)
                if len(txns) > 10:
                    logger.info("[dry-run]   ... +%d more", len(txns) - 10)
                parsed += 1
                continue

            # --- Horizons HTTP API path ---
            if use_hz:
                member = hz.lookup_member_by_name(
                    rec["name"],
                    state_district=rec.get("state_district"),
                )
                # Live may lack a members roster while still accepting filings
                # (member_id optional; bioguide + denormalized name still useful).
                if not member:
                    logger.warning(
                        "No Horizons member match for filer name=%r "
                        "state_district=%r — submitting without member_id",
                        rec["name"],
                        rec.get("state_district"),
                    )
                filing = {
                    "chamber": "house",
                    "filing_type": pb_filing_type,
                    "filing_date": rec["filing_date"],
                    "source_url": rec["pdf_url"],
                    "raw_document_type": "pdf",
                    "parse_status": parse_status,
                    "member_name": (member or {}).get("name") or rec["name"],
                    "member_party": (member or {}).get("party") or "",
                    "member_state": (member or {}).get("state") or state_code or "",
                }
                if member and member.get("id"):
                    filing["member_id"] = member["id"]
                bioguide = (member or {}).get("bioguide_id") or ""
                if bioguide:
                    filing["bioguide_id"] = bioguide
                result = hz.submit_filing(filing, transactions=txns)
                logger.info(
                    "House submitted filing_id=%s txns=%s name=%r",
                    result.get("filing_id"),
                    result.get("transactions_written"),
                    rec["name"],
                )
                parsed += 1
                continue

            # --- Legacy PocketBase direct path ---
            member_id = None
            member_meta = {}
            if conn is not None:
                member_id = db.find_member_id(
                    conn,
                    rec["name"],
                    state_district=rec.get("state_district"),
                    chamber="house",
                )
                if member_id is None:
                    logger.warning(
                        "No member match for filer name: %s (state_district=%s) — "
                        "skipping, needs manual review",
                        rec["name"],
                        rec.get("state_district"),
                    )
                    failed += 1
                    continue
                member_meta = db.get_member(conn, member_id) or {}

            filing = {
                "member_id": member_id,
                "chamber": "house",
                "filing_type": pb_filing_type,
                "filing_date": rec["filing_date"],
                "source_url": rec["pdf_url"],
                "raw_document_type": "pdf",
                "parse_status": parse_status,
                "member_name": member_meta.get("name") or rec["name"],
                "member_party": member_meta.get("party") or "",
                "member_state": member_meta.get("state") or state_code or "",
            }
            filing_id = db.upsert_filing(conn, filing)
            for txn in txns:
                txn.update(
                    {
                        "filing_id": filing_id,
                        "member_id": member_id,
                        "ticker": None,
                        "ticker_confidence": "unconfirmed",
                        "amount_range_low": None,
                        "amount_range_high": None,
                        "asset_category": None,
                    }
                )
                db.upsert_transaction(conn, txn)

            parsed += 1
        except Exception as e:
            logger.error(
                "Failed to process House filing %s: %s", rec.get("pdf_url"), e
            )
            failed += 1

    if use_hz:
        hz.submit_ingestion_log("house_clerk", found, parsed, failed)
    elif conn is not None and not dry_run:
        db.log_ingestion_run(conn, "house_clerk", found, parsed, failed)

    return {"found": found, "parsed": parsed, "failed": failed}
