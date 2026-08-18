"""AO / JEFS judiciary financial-disclosure ingest (ao-judicial-disclosures-sync).

Live-verified access pattern (2026-08) at https://pub.jefs.uscourts.gov/ :

  1. Landing page is registration-only. Search UI is not served until a
     session is registered. ``robots.txt`` is ``User-agent: * Allow: /``.
  2. Users must register **each visit** (5 U.S.C. § 13107) with real name,
     occupation, mailing address, and "Requesting on Behalf of". The form
     is POST /index.php action=register + CSRF. Google reCAPTCHA is on
     the page; the site JS calls register() even when verify_recaptcha
     fails. We do not solve captchas and we do not invent an identity.
  3. POST /index.php action=search returns JSON rows **only after login**.
     Unauthenticated search is ``{"document":"You must be logged in"}``.
     POST action=get_facets works without login (years, report types,
     courts, positions).
  4. There is no public per-filing permalink. Downloads are a session cart:
     POST action=add|remove &id=… then GET /index.php?action=download
     (max 100). Reports are JEFS-generated PDFs (Form AO 10 annual /
     Form AO 10T PTR), not HTML tables.
  5. F5 WAF rejects some GET query strings; use POST for search.

judge_id is the FJC nid from fjc-judges-sync. Ambiguous name matches
go to manual_review. Dollar fields are disclosed brackets only — never
a midpoint.
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import os
import re
import sys
import time
import zipfile
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import config
from .fjc import download_csv as download_fjc_csv
from .fjc import map_row as map_fjc_row
from .fjc import parse_csv as parse_fjc_csv
from .ingest_client import get_client
from .names import STATE_CODES, normalize_state, parse_filer_name

logger = logging.getLogger("rollcalltrack_ingest.jefs")

JEFS_BASE = "https://pub.jefs.uscourts.gov"
JEFS_INDEX = f"{JEFS_BASE}/index.php"
PAGE_DELAY_SECONDS = 1.5
SEARCH_PAGE_SIZE = 10
HORIZONS_BATCH_SIZE = 10

FILING_TYPE_ENUM = ("annual", "periodic_transaction")
TXN_TYPE_ENUM = ("purchase", "sale", "exchange")

# 5 U.S.C. § 13104(d) brackets + the J–P4 letter codes used on AO-10/AO-10T.
# high=None means genuinely open-ended. Never invent a midpoint.
VALUE_CODES: dict[str, tuple[int, int | None]] = {
    "J": (1, 15_000),
    "K": (15_001, 50_000),
    "L": (50_001, 100_000),
    "M": (100_001, 250_000),
    "N": (250_001, 500_000),
    "O": (500_001, 1_000_000),
    "P1": (1_000_001, 5_000_000),
    "P2": (5_000_001, 25_000_000),
    "P3": (25_000_001, 50_000_000),
    "P4": (50_000_001, None),
}

RANGE_RE = re.compile(
    r"\$\s*([\d,]+(?:\.\d+)?)\s*(?:-|to|–|—)\s*\$\s*([\d,]+(?:\.\d+)?)",
    re.I,
)
MORE_THAN_RE = re.compile(
    r"(?:more than|greater than|over)\s+\$\s*([\d,]+(?:\.\d+)?)",
    re.I,
)
OR_MORE_RE = re.compile(
    r"\$\s*([\d,]+(?:\.\d+)?)\s+or more",
    re.I,
)
CODE_RE = re.compile(r"\b(P[1-4]|[JKLMNO])\b")

TXN_TYPE_RE = re.compile(
    r"\b(buy(?:\s*\(add['’]?l\))?|sold(?:\s*\(part\))?|sale|purchase|"
    r"redeemed|redeem|exchange)\b",
    re.I,
)
DATE_RE = re.compile(r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{2}-\d{2})\b")

STOP_COURT_TOKENS = frozenset(
    {
        "court",
        "united",
        "states",
        "district",
        "circuit",
        "appeals",
        "judge",
        "judicial",
        "the",
        "for",
        "of",
        "and",
    }
)


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": config.USER_AGENT})
    retry = Retry(
        total=5,
        connect=3,
        read=3,
        status=5,
        backoff_factor=2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def iso_date(value: Any) -> str | None:
    s = ("" if value is None else str(value)).strip()
    if not s:
        return None
    if "T" in s:
        s = s.split("T", 1)[0]
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%m/%d/%y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_amount_range(text: str | None) -> tuple[int | None, int | None]:
    """Return (low, high) from a disclosed bracket or AO-10 letter code.

    Open-ended highs are None. Midpoints are never computed.
    """
    if not text:
        return None, None
    s = re.sub(r"\s+", " ", text).strip()
    m = RANGE_RE.search(s)
    if m:
        return int(float(m.group(1).replace(",", ""))), int(
            float(m.group(2).replace(",", ""))
        )
    m = OR_MORE_RE.search(s) or MORE_THAN_RE.search(s)
    if m:
        # Stated floor only. Do not add 1 — that would invent a bound.
        return int(float(m.group(1).replace(",", ""))), None
    # Prefer longer codes (P1) over a lone P.
    codes = CODE_RE.findall(s)
    if len(codes) == 1 and codes[0] in VALUE_CODES:
        return VALUE_CODES[codes[0]]
    return None, None


def map_filing_type(report_type: str | None) -> str | None:
    raw = (report_type or "").strip().lower()
    if raw == "periodic transaction report":
        return "periodic_transaction"
    # Initial / Final / Annual/Final are the same AO-10 asset form.
    if raw in ("annual", "annual/final", "final", "initial"):
        return "annual"
    return None


def map_txn_type(raw: str | None) -> str | None:
    s = (raw or "").strip().lower()
    if not s:
        return None
    if s.startswith("buy") or s.startswith("purchase"):
        return "purchase"
    if s.startswith("sold") or s.startswith("sale") or s.startswith("redeem"):
        return "sale"
    if s.startswith("exchange"):
        return "exchange"
    return None


def source_url_for(jefs_id: str) -> str:
    """Stable locator. JEFS has no unauthenticated permalink (see README)."""
    return f"{JEFS_BASE}/?filing={quote(str(jefs_id), safe='')}"


def jefs_identity_from_env() -> dict[str, str] | None:
    """Statutory requester identity. Missing fields → None (do not invent)."""
    first = os.environ.get("JEFS_FIRST_NAME", "").strip()
    last = os.environ.get("JEFS_LAST_NAME", "").strip()
    occupation = os.environ.get("JEFS_OCCUPATION", "").strip()
    addr1 = os.environ.get("JEFS_ADDRESS1", "").strip()
    city = os.environ.get("JEFS_CITY", "").strip()
    state = os.environ.get("JEFS_STATE", "").strip()
    postal = os.environ.get("JEFS_ZIP", "").strip()
    representing = os.environ.get("JEFS_REPRESENTING", "").strip() or "self"
    if not all([first, last, occupation, addr1, city, state, postal]):
        return None
    code = normalize_state(state)
    state_out = STATE_CODES[code].title() if code and code in STATE_CODES else state
    return {
        "first_name": first,
        "last_name": last,
        "occupation": occupation,
        "email": os.environ.get("JEFS_EMAIL", "").strip(),
        "phone": os.environ.get("JEFS_PHONE", "").strip(),
        "address-line-1": addr1,
        "address-line-2": os.environ.get("JEFS_ADDRESS2", "").strip(),
        "address-city": city,
        "address-state": state_out,
        "address-postalcode": postal,
        "representing": representing,
        "representing-address": os.environ.get("JEFS_REPRESENTING_ADDRESS", "").strip(),
    }


# Live JEFS #registration-form names only. Extra keys (e.g. address-country)
# make the server return {status: 0, message: "Issues in registration"}.
REGISTER_FIELD_NAMES = (
    "first_name",
    "last_name",
    "occupation",
    "email",
    "phone",
    "address-line-1",
    "address-line-2",
    "address-city",
    "address-state",
    "address-postalcode",
    "representing",
    "representing-address",
)


def register_form_payload(csrf: str, identity: dict[str, str]) -> dict[str, str]:
    payload = {
        "action": "register",
        "_csrf": csrf,
        "phone_validated": "0",
        "certified": "on",
    }
    for key in REGISTER_FIELD_NAMES:
        payload[key] = identity.get(key, "") or ""
    return payload


def register(session: requests.Session, identity: dict[str, str]) -> None:
    home = session.get(JEFS_BASE + "/", timeout=config.REQUEST_TIMEOUT_SECONDS)
    home.raise_for_status()
    csrf_m = re.search(r'name="_csrf"\s+value="([^"]+)"', home.text)
    if not csrf_m:
        raise RuntimeError("JEFS registration page missing _csrf — form may have changed")
    payload = register_form_payload(csrf_m.group(1), identity)
    resp = session.post(
        JEFS_INDEX,
        data=payload,
        timeout=config.REQUEST_TIMEOUT_SECONDS,
        headers={"Referer": JEFS_BASE + "/", "X-Requested-With": "XMLHttpRequest"},
    )
    resp.raise_for_status()
    try:
        data = resp.json()
    except ValueError as e:
        raise RuntimeError(f"JEFS register returned non-JSON: {resp.text[:200]}") from e
    if data.get("status") in (0, "0") or data.get("document") == "You must be logged in":
        raise RuntimeError(f"JEFS register rejected: {data}")
    logger.info("JEFS registration accepted")


def search_page(
    session: requests.Session,
    q: str = "",
    facets: dict | None = None,
    start: int = 0,
    sort_by: str = "f.published_dt",
    sort_dir: str = "DESC",
) -> dict:
    payload = {
        "action": "search",
        "q": q,
        "start": str(start),
        "sort_by": sort_by,
        "sort_dir": sort_dir,
        "facet_string": json.dumps(facets or {}),
    }
    resp = session.post(
        JEFS_INDEX,
        data=payload,
        timeout=config.REQUEST_TIMEOUT_SECONDS,
        headers={"Referer": JEFS_BASE + "/", "X-Requested-With": "XMLHttpRequest"},
    )
    resp.raise_for_status()
    try:
        data = resp.json()
    except ValueError as e:
        raise RuntimeError(f"JEFS search returned non-JSON: {resp.text[:200]}") from e
    if isinstance(data, dict) and data.get("document") == "You must be logged in":
        raise RuntimeError("JEFS search requires a registered session")
    return data


def search_filings(
    session: requests.Session,
    q: str = "",
    facets: dict | None = None,
    since: date | None = None,
    max_records: int | None = None,
) -> list[dict]:
    """Paginate JEFS search. ``since`` filters on published / certified date."""
    out: list[dict] = []
    start = 0
    while True:
        data = search_page(session, q=q, facets=facets, start=start)
        rows = data.get("rows") or []
        if not rows:
            break
        older_page = True
        for row in rows:
            rec = _normalize_search_row(row)
            rec_date = iso_date(rec.get("filing_date"))
            if since and rec_date and rec_date < since.isoformat():
                continue
            older_page = False
            out.append(rec)
            if max_records is not None and len(out) >= max_records:
                return out
        filercount = int(data.get("filercount") or 0)
        users_here = len({r.get("filer_id_FK") for r in rows})
        if start + users_here >= filercount:
            break
        if since and older_page:
            break
        start += SEARCH_PAGE_SIZE
        time.sleep(PAGE_DELAY_SECONDS)
    return out


def _normalize_search_row(row: dict) -> dict:
    report_type = (row.get("report_type_s") or "").strip()
    filing_type = map_filing_type(report_type)
    if report_type == "Periodic Transaction Report":
        filing_date = iso_date(row.get("ao_certified_dt")) or iso_date(
            row.get("published_dt")
        )
    else:
        filing_date = iso_date(row.get("published_dt")) or iso_date(
            row.get("ao_certified_dt")
        )
    jefs_id = str(row.get("id") or "").strip()
    return {
        "jefs_id": jefs_id,
        "filer_id": row.get("filer_id_FK"),
        "name": (row.get("name_s") or "").strip(),
        "court": (row.get("court_type_s") or "").strip(),
        "position": (row.get("position_s") or "").strip(),
        "report_type": report_type,
        "filing_type": filing_type,
        "filing_date": filing_date,
        "operating_year": (row.get("operating_year_s") or "").strip(),
        "amendment": row.get("amendment_number_s") or "",
        "source_url": source_url_for(jefs_id) if jefs_id else "",
        "raw": row,
    }


def court_compatible(jefs_court: str | None, fjc_court: str | None) -> bool:
    a = (jefs_court or "").lower()
    b = (fjc_court or "").lower()
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    tokens = [
        t
        for t in re.split(r"[^a-z0-9]+", a)
        if len(t) > 3 and t not in STOP_COURT_TOKENS
    ]
    return bool(tokens) and all(t in b for t in tokens)


def match_judge(jefs_name: str, jefs_court: str | None, roster: list[dict]) -> dict | None:
    """Return the unique roster hit, or None if missing/ambiguous.

    Last name must match. First name must match or be a ≥3-char prefix.
    Multiple last+first hits are reduced by court; leftover ties are None.
    """
    parsed = parse_filer_name(jefs_name)
    if not parsed.last:
        return None
    first = parsed.first.lower()
    last = parsed.last.lower()
    hits: list[dict] = []
    for judge in roster:
        jp = parse_filer_name(judge.get("full_name") or "")
        if jp.last.lower() != last:
            continue
        jf = jp.first.lower()
        if first and jf:
            if first != jf and not (
                len(first) >= 3 and jf.startswith(first)
            ) and not (len(jf) >= 3 and first.startswith(jf)):
                continue
        hits.append(judge)
    if not hits:
        return None
    if len(hits) == 1:
        return hits[0]
    if jefs_court:
        court_hits = [h for h in hits if court_compatible(jefs_court, h.get("court"))]
        if len(court_hits) == 1:
            return court_hits[0]
        if len(court_hits) > 1:
            return None
    return None


def load_judges(
    judge_id: str | None = None,
    client=None,
    fjc_csv_text: str | None = None,
    roster: list[dict] | None = None,
) -> list[dict]:
    """Horizons GET /judicial/judges if it exists; else the live FJC export."""
    if roster is not None:
        loaded = list(roster)
        if judge_id:
            wanted = str(judge_id).strip()
            loaded = [j for j in loaded if str(j.get("judge_id")) == wanted]
            if not loaded:
                raise ValueError(f"judge_id={wanted} not in provided roster")
        return loaded
    roster = []
    hz = client
    if hz is None:
        try:
            hz = get_client()
        except Exception:
            hz = None
    if hz is not None and getattr(hz, "configured", False):
        try:
            resp = hz._get("/hcgi/api/judicial/judges")
            if resp.status_code == 200:
                body = resp.json()
                roster = body.get("judges") or body.get("items") or []
                logger.info("Loaded %d judges from Horizons", len(roster))
            else:
                logger.info(
                    "Horizons GET /hcgi/api/judicial/judges HTTP %s — "
                    "falling back to FJC CSV",
                    resp.status_code,
                )
        except Exception as e:
            logger.info("Horizons judges read failed (%s) — falling back to FJC CSV", e)

    if not roster:
        text = fjc_csv_text if fjc_csv_text is not None else download_fjc_csv()
        for row in parse_fjc_csv(text):
            rec = map_fjc_row(row)
            if rec.get("_review"):
                continue
            roster.append(rec)
        logger.info("Loaded %d judges from FJC export", len(roster))

    if judge_id:
        wanted = str(judge_id).strip()
        roster = [j for j in roster if str(j.get("judge_id")) == wanted]
        if not roster:
            raise ValueError(f"judge_id={wanted} not in FJC/Horizons roster")
    return roster


def parse_ptr_text(text: str, filing_id: str, source_url: str) -> list[dict]:
    """Extract AO-10T transaction rows from PDF text."""
    txns: list[dict] = []
    for raw_line in text.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line or not TXN_TYPE_RE.search(line):
            continue
        dates = DATE_RE.findall(line)
        if not dates:
            continue
        type_m = TXN_TYPE_RE.search(line)
        ttype = map_txn_type(type_m.group(1) if type_m else "")
        if ttype is None:
            continue
        low, high = parse_amount_range(line)
        asset = line[: type_m.start()].strip(" -–—|,")
        asset = re.sub(r"^\d+\s+", "", asset)
        if len(asset) < 2:
            continue
        if any(
            bad in asset.lower()
            for bad in ("form ao", "periodic transaction", "description of asset")
        ):
            continue
        txns.append(
            {
                "filing_id": filing_id,
                "asset_name": asset,
                "transaction_type": ttype,
                "transaction_date": iso_date(dates[0]),
                "amount_range_low": low,
                "amount_range_high": high,
                "source_url": source_url,
            }
        )
    return txns


def parse_annual_assets(text: str, filing_id: str, source_url: str) -> list[dict]:
    """Extract Part VII-style asset rows from an AO-10 PDF.

    JEFS layout is not public without a session. We only keep a line when a
    disclosed value bracket or a single value code is present. No guessing.
    """
    assets: list[dict] = []
    in_part_vii = False
    for raw_line in text.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if re.search(r"part\s+vii", line, re.I):
            in_part_vii = True
            continue
        if re.search(r"part\s+viii", line, re.I):
            break
        if not in_part_vii and not RANGE_RE.search(line) and not CODE_RE.search(line):
            continue
        low, high = parse_amount_range(line)
        if low is None and high is None:
            continue
        name = RANGE_RE.sub("", line)
        name = CODE_RE.sub("", name)
        name = re.sub(r"\s+", " ", name).strip(" -–—|,")
        if len(name) < 2:
            continue
        if any(
            bad in name.lower()
            for bad in ("gross value", "investments and trusts", "value method")
        ):
            continue
        assets.append(
            {
                "filing_id": filing_id,
                "asset_name": name,
                "asset_type": None,
                "value_range_low": low,
                "value_range_high": high,
                "source_url": source_url,
            }
        )
    return assets


def extract_pdf_text(blob: bytes) -> str:
    import pdfplumber

    parts: list[str] = []
    with pdfplumber.open(io.BytesIO(blob)) as pdf:
        for page in pdf.pages:
            parts.append(page.extract_text() or "")
    return "\n".join(parts)


def download_pdf(session: requests.Session, jefs_id: str) -> bytes | None:
    session.post(
        JEFS_INDEX,
        data={"action": "add", "id": jefs_id},
        timeout=config.REQUEST_TIMEOUT_SECONDS,
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    resp = session.get(
        JEFS_INDEX,
        params={"action": "download"},
        timeout=config.REQUEST_TIMEOUT_SECONDS,
    )
    session.post(
        JEFS_INDEX,
        data={"action": "remove", "id": jefs_id},
        timeout=config.REQUEST_TIMEOUT_SECONDS,
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    if resp.status_code != 200:
        logger.warning("JEFS download HTTP %s for id=%s", resp.status_code, jefs_id)
        return None
    if "must be registered" in (resp.text or "").lower():
        raise RuntimeError("JEFS download requires a registered session")
    ctype = (resp.headers.get("content-type") or "").lower()
    body = resp.content
    if "pdf" in ctype or body[:4] == b"%PDF":
        return body
    if "zip" in ctype or body[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(body)) as zf:
            for name in zf.namelist():
                raw = zf.read(name)
                if raw[:4] == b"%PDF":
                    return raw
        return None
    logger.warning("JEFS download id=%s was not a PDF (%s)", jefs_id, ctype)
    return None


def build_payload(row: dict, judge: dict, pdf_text: str | None) -> dict:
    filing_id = row["jefs_id"]
    source_url = row["source_url"]
    filing = {
        "judge_id": judge["judge_id"],
        "filing_id": filing_id,
        "filing_type": row["filing_type"],
        "filing_date": row["filing_date"],
        "source_url": source_url,
    }
    transactions: list[dict] = []
    annual_assets: list[dict] = []
    parse_ok = True
    if pdf_text is None:
        parse_ok = False
    elif row["filing_type"] == "periodic_transaction":
        transactions = parse_ptr_text(pdf_text, filing_id, source_url)
        if not transactions:
            parse_ok = False
    elif row["filing_type"] == "annual":
        annual_assets = parse_annual_assets(pdf_text, filing_id, source_url)
        if not annual_assets:
            parse_ok = False
    return {
        "filing": filing,
        "transactions": transactions,
        "annual_assets": annual_assets,
        "_parse_ok": parse_ok,
    }


def run(
    judge_id: str | None = None,
    days_back: int = 14,
    max_records: int | None = None,
    dry_run: bool = False,
    client=None,
    fjc_csv_text: str | None = None,
    search_rows: list[dict] | None = None,
    pdf_text_by_id: dict[str, str] | None = None,
    roster: list[dict] | None = None,
) -> dict:
    roster = load_judges(
        judge_id=judge_id,
        client=client,
        fjc_csv_text=fjc_csv_text,
        roster=roster,
    )
    since = date.today() - timedelta(days=days_back)

    rows = search_rows
    if rows is None:
        identity = jefs_identity_from_env()
        cookie = os.environ.get("JEFS_SESSION", "").strip()
        if not identity and not cookie:
            raise RuntimeError(
                "JEFS search needs a statutory requester identity "
                "(JEFS_FIRST_NAME, JEFS_LAST_NAME, JEFS_OCCUPATION, "
                "JEFS_ADDRESS1, JEFS_CITY, JEFS_STATE, JEFS_ZIP) or JEFS_SESSION. "
                "Do not invent these. See README."
            )
        session = _session()
        if cookie:
            session.cookies.set("SESSID", cookie, domain="pub.jefs.uscourts.gov")
        else:
            register(session, identity)  # type: ignore[arg-type]
        facets = {
            "report_type_s": [
                "Periodic Transaction Report",
                "Annual",
                "Annual/Final",
            ]
        }
        q = ""
        if judge_id:
            # Search the roster name, not the nid — JEFS has no FJC id field.
            q = roster[0].get("full_name") or ""
            facets = None
        rows = search_filings(
            session,
            q=q,
            facets=facets,
            since=since,
            max_records=max_records,
        )
    else:
        session = None
        if max_records is not None:
            rows = rows[:max_records]

    mapped: list[dict] = []
    review: list[dict] = []
    for row in rows:
        if not row.get("jefs_id") or not row.get("source_url"):
            review.append(
                {
                    "_review": True,
                    "reason": "missing_source_url",
                    "notes": "JEFS row missing id",
                    "name": row.get("name"),
                }
            )
            continue
        if row.get("filing_type") not in FILING_TYPE_ENUM:
            review.append(
                {
                    "_review": True,
                    "reason": "unexpected_structure",
                    "notes": f"unmapped report_type={row.get('report_type')!r}",
                    "jefs_id": row.get("jefs_id"),
                    "name": row.get("name"),
                    "source_url": row.get("source_url"),
                }
            )
            continue
        if not row.get("filing_date"):
            review.append(
                {
                    "_review": True,
                    "reason": "unexpected_structure",
                    "notes": "missing filing_date",
                    "jefs_id": row.get("jefs_id"),
                    "name": row.get("name"),
                    "source_url": row.get("source_url"),
                }
            )
            continue
        judge = match_judge(row.get("name") or "", row.get("court"), roster)
        if judge is None:
            review.append(
                {
                    "_review": True,
                    "reason": "unexpected_structure",
                    "notes": (
                        f"ambiguous or missing FJC match for name={row.get('name')!r} "
                        f"court={row.get('court')!r}"
                    ),
                    "jefs_id": row.get("jefs_id"),
                    "name": row.get("name"),
                    "source_url": row.get("source_url"),
                }
            )
            continue

        pdf_text = None
        if pdf_text_by_id is not None:
            pdf_text = pdf_text_by_id.get(row["jefs_id"])
        elif session is not None:
            blob = download_pdf(session, row["jefs_id"])
            time.sleep(PAGE_DELAY_SECONDS)
            if blob:
                try:
                    pdf_text = extract_pdf_text(blob)
                except Exception as e:
                    logger.warning("PDF extract failed id=%s: %s", row["jefs_id"], e)
                    pdf_text = None

        payload = build_payload(row, judge, pdf_text)
        if not payload["_parse_ok"]:
            review.append(
                {
                    "_review": True,
                    "reason": "unexpected_structure",
                    "notes": (
                        f"could not parse {row['filing_type']} PDF for "
                        f"id={row['jefs_id']}"
                    ),
                    "jefs_id": row["jefs_id"],
                    "judge_id": judge["judge_id"],
                    "source_url": row["source_url"],
                    "filing": payload["filing"],
                }
            )
            # Still keep a confidently matched filing shell so Horizons has
            # the cover record; transactions/assets stay empty.
        mapped.append(
            {
                **payload["filing"],
                "transactions": payload["transactions"],
                "annual_assets": payload["annual_assets"],
            }
        )

    logger.info(
        "jefs found=%s mapped=%s review=%s days_back=%s judge_id=%s",
        len(rows),
        len(mapped),
        len(review),
        days_back,
        judge_id,
    )

    posted = 0
    if dry_run:
        for rec in mapped[:5]:
            logger.info("dry-run filing %s", json.dumps(rec, default=str)[:600])
        for rec in review[:5]:
            logger.info("dry-run review %s", json.dumps(rec, default=str)[:500])
    else:
        hz = client or get_client()
        if not hz.configured:
            raise RuntimeError("HORIZONS_BASE_URL and INGEST_API_KEY required")
        for i in range(0, len(mapped), HORIZONS_BATCH_SIZE):
            hz.submit_judicial_disclosures(mapped[i : i + HORIZONS_BATCH_SIZE])
            posted += len(mapped[i : i + HORIZONS_BATCH_SIZE])
        for rec in review:
            hz.flag_manual_review(
                filing_id=rec.get("jefs_id") or rec.get("source_url") or "unknown",
                reason=rec.get("reason") or "unexpected_structure",
                notes=rec.get("notes") or "",
            )
        hz.submit_ingestion_log(
            source="jefs",
            found=len(rows),
            parsed=len(mapped),
            failed=len(review),
            flagged=len(review),
            note=f"days_back={days_back} judge_id={judge_id or '-'}",
        )

    return {
        "found": len(rows),
        "mapped": len(mapped),
        "review": len(review),
        "posted": posted,
        "dry_run": dry_run,
        "days_back": days_back,
        "judge_id": judge_id,
        "roster": len(roster),
    }


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="AO / JEFS judiciary financial-disclosure ingest"
    )
    p.add_argument(
        "--judge-id",
        default=None,
        help="FJC nid; search that judge only (uses roster name, not nid)",
    )
    p.add_argument("--days-back", type=int, default=14)
    p.add_argument("--max-records", type=int, default=None)
    p.add_argument("--dry-run", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = build_arg_parser().parse_args(argv)
    try:
        config.require_config(dry_run=args.dry_run, source="jefs")
    except RuntimeError as e:
        logger.error(str(e))
        return 1
    try:
        result = run(
            judge_id=args.judge_id,
            days_back=args.days_back,
            max_records=args.max_records,
            dry_run=args.dry_run,
        )
    except (RuntimeError, ValueError) as e:
        logger.error(str(e))
        return 1
    logger.info("=== jefs summary === %s", result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
