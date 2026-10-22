"""Horizons HTTP client for member lookup + disclosure ingest.

Live-verified (2026-08-05) against:
  GET  {base}/hcgi/api/members/lookup
  POST {base}/hcgi/api/ingest/disclosures  (header X-Ingest-Key)

Submit body shape:
  { "type": "filing", "payload": { "filing": {...}, "transactions": [...] } }
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any
from urllib.parse import quote, urlencode

import requests

from . import config
from .guards import assert_remote_post_allowed, skip_reason
from .http_retry import backoff_seconds, is_connection_error
from .legislators import match_legislator
from .names import (
    is_bare_office_title,
    member_record_compatible,
    normalized_lookup_names,
    parse_state_district,
    state_for_member_lookup,
    state_from_office,
)

# Gap between member lookups. The disclosure run was tripping Horizons 429s
# by calling /members/lookup on every filing with no pause.
LOOKUP_MIN_INTERVAL = 0.4
LOOKUP_ATTEMPTS = 5
LOOKUP_BACKOFF_BASE = 2.0
LOOKUP_BACKOFF_CAP = 30.0
INGEST_ATTEMPTS = 6
INGEST_BACKOFF_BASE = 5.0
INGEST_BACKOFF_CAP = 90.0

logger = logging.getLogger("rollcalltrack_ingest.ingest_client")

_PB_META = ("collectionId", "collectionName", "created", "updated")
# Live reads advertise `ratelimit-policy: 100;w=300` (100 requests / 5 minutes).
# When those headers are absent, stay under that budget with a fixed gap.
_LIST_GET_INTERVAL_SECONDS = 3.1
_RATE_LIMIT_RESERVE = 2
_LIST_RETRY_STATUS = frozenset({403, 429, 500, 502, 503, 504})


class DisclosureListError(RuntimeError):
    """Raised when no disclosure list route returns filings.

    A missing route must not look like an empty catalog. An empty catalog
    makes the backfill report zero changes and exit 0.
    """


def _without_server_meta(row: dict) -> dict:
    return {key: value for key, value in row.items() if key not in _PB_META}


def _row_id_is_bioguide(member: dict) -> bool:
    """True when this row's id is a congressional bioguide, not a database id."""
    member_id = str(member.get("id") or "").strip()
    bioguide = str(member.get("bioguide_id") or "").strip()
    if not member_id:
        return False
    if bioguide and member_id.upper() == bioguide.upper():
        return True
    return bool(re.fullmatch(r"[A-Za-z]\d{6}", member_id))


class LookupRateLimited(RuntimeError):
    """Member lookup still got HTTP 429 after retries. That is not a miss."""


def _norm_lookup_part(value) -> str:
    return " ".join(str(value or "").split()).casefold()


def _member_lookup_cache_key(params: dict) -> tuple[str, str, str, str]:
    """Normalized name, state, and chamber. Bioguide stays in the key too."""
    state_raw = params.get("state")
    canonical = state_for_member_lookup(str(state_raw)) if state_raw else None
    return (
        _norm_lookup_part(params.get("name")),
        _norm_lookup_part(canonical or state_raw),
        _norm_lookup_part(params.get("chamber")),
        _norm_lookup_part(params.get("bioguide_id")),
    )


def _ingestion_response_body(resp, limit: int = 2000) -> str:
    text = (getattr(resp, "text", "") or "").strip()
    if len(text) > limit:
        return text[:limit] + "…"
    return text or "<empty>"


def _map_txn_type(raw: str | None) -> str:
    s = (raw or "other").strip().lower()
    if s in ("purchase", "sale", "exchange", "other"):
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
    if s in ("self", "spouse", "dependent", "joint"):
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
    if s in ("high", "medium", "low", "none"):
        return s
    if s in ("filed", "confirmed", "exact"):
        return "high"
    if s in ("unconfirmed",):
        return "low"
    return "none"


def _with_roster_party(member: dict, local: dict | None) -> dict:
    """Keep a Horizons row. Fill party from the roster only when it is blank."""
    if not local or str(member.get("party") or "").strip() or not local.get("party"):
        return member
    enriched = dict(member)
    enriched["party"] = local["party"]
    return enriched


def clean_transaction(txn: dict) -> dict:
    """Map scraper fields onto Horizons/PB select enums."""
    out: dict[str, Any] = {
        "asset_name": txn.get("asset_name") or "",
        "filer_relation": _map_filer_relation(txn.get("filer_relation")),
        "transaction_type": _map_txn_type(txn.get("transaction_type")),
        "transaction_date": txn.get("transaction_date") or "",
        "amount_range_raw": txn.get("amount_range_raw") or "",
        "ticker": (txn.get("ticker") or "").strip(),
        "ticker_confidence": _map_ticker_conf(txn.get("ticker_confidence")),
    }
    if txn.get("line_index") is not None:
        out["line_index"] = int(txn["line_index"])
    if txn.get("notification_date"):
        out["notification_date"] = txn["notification_date"]
    if txn.get("asset_category"):
        out["asset_category"] = txn["asset_category"]
    if txn.get("amount_range_low") is not None:
        out["amount_range_low"] = txn["amount_range_low"]
    if txn.get("amount_range_high") is not None:
        out["amount_range_high"] = txn["amount_range_high"]
    # Omit blanks. An empty member_id on update clears a link already stored.
    member_id = str(txn.get("member_id") or "").strip()
    if member_id:
        out["member_id"] = member_id
    member_party = str(txn.get("member_party") or txn.get("party") or "").strip()
    if member_party:
        out["member_party"] = member_party
    return out


def _lookup_name_candidates(full_name: str, local: dict | None) -> list[str]:
    """Names to try, roster official/nickname first, then the filing form.

    congress-legislators ``official_full`` is what Horizons usually stores
    (``Mitch McConnell``, ``Bernie Moreno``). The filing may say
    ``A. Mitchell`` or ``Bernardo``.
    """
    names: list[str] = []

    def add(value: str | None) -> None:
        text = " ".join((value or "").split())
        if not text or is_bare_office_title(text):
            return
        if any(text.lower() == existing.lower() for existing in names):
            return
        names.append(text)

    if local:
        add(local.get("official_full"))
        nickname = str(local.get("nickname") or "").strip()
        first = str(local.get("first") or "").strip()
        last = str(local.get("last") or "").strip()
        if nickname and last:
            add(f"{nickname} {last}")
        if first and last:
            add(f"{first} {last}")
    add(full_name)
    return names


class HorizonsIngestClient:
    """HTTP client for Horizons members lookup + ingest/disclosures."""

    def __init__(
        self,
        base_url: str | None = None,
        ingest_key: str | None = None,
        timeout: int | None = None,
    ):
        self.base = (base_url or config.HORIZONS_BASE_URL or "").rstrip("/")
        self.ingest_key = ingest_key or config.INGEST_API_KEY or ""
        self.timeout = timeout or config.REQUEST_TIMEOUT_SECONDS
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": config.USER_AGENT})
        self._min_get_interval = 0.0
        self._last_get_at = 0.0
        self._ratelimit_remaining = None
        self._ratelimit_reset = None
        self._saw_rate_limit = False
        # Name/state/chamber → member or None. Lives for this client (one run).
        self._lookup_cache: dict[tuple[str, str, str, str], dict | None] = {}

    @property
    def configured(self) -> bool:
        return bool(self.base and self.ingest_key)

    def _header_value(self, resp, name: str):
        headers = getattr(resp, "headers", None) or {}
        getter = getattr(headers, "get", None)
        if getter is None:
            return None
        for key in (name, name.lower(), name.title()):
            value = getter(key)
            if value is not None and str(value).strip() != "":
                return value
        return None

    def _note_rate_limit(self, resp) -> None:
        """Remember `ratelimit-remaining` / `ratelimit-reset` from a public GET."""
        remaining = self._header_value(resp, "ratelimit-remaining")
        reset = self._header_value(resp, "ratelimit-reset")
        try:
            self._ratelimit_remaining = int(str(remaining).strip())
            self._saw_rate_limit = True
        except (TypeError, ValueError):
            self._ratelimit_remaining = None
        try:
            reset_s = float(str(reset).strip())
            if reset_s > 10000:
                reset_s = max(0.0, reset_s - time.time())
            self._ratelimit_reset = reset_s
            self._saw_rate_limit = True
        except (TypeError, ValueError):
            self._ratelimit_reset = None
        if not self._saw_rate_limit and float(self._min_get_interval or 0) <= 0:
            self._min_get_interval = _LIST_GET_INTERVAL_SECONDS

    def _pace_get(self) -> None:
        remaining = getattr(self, "_ratelimit_remaining", None)
        if remaining is not None and remaining <= _RATE_LIMIT_RESERVE:
            reset = getattr(self, "_ratelimit_reset", None)
            wait = 60.0 if reset is None else min(max(float(reset), 1.0), 300.0)
            logger.info(
                "public rate limit remaining=%s, pausing %.0fs",
                remaining,
                wait,
            )
            time.sleep(wait)
            self._ratelimit_remaining = None
            self._ratelimit_reset = None
            self._last_get_at = time.monotonic()
            return
        interval = float(getattr(self, "_min_get_interval", 0) or 0)
        if interval <= 0:
            return
        now = time.monotonic()
        wait = self._last_get_at + interval - now
        if wait > 0:
            time.sleep(wait)
        self._last_get_at = time.monotonic()

    def _get(self, path: str, params: dict | None = None) -> requests.Response:
        self._pace_get()
        url = f"{self.base}{path}"
        if params:
            url = f"{url}?{urlencode(params)}"
        resp = self.session.get(url, timeout=self.timeout)
        self._note_rate_limit(resp)
        return resp

    def _should_retry(self, resp: requests.Response) -> bool:
        if resp.status_code == 429:
            return True
        if resp.status_code == 403:
            body = (resp.text or "").lower()
            return "too many" in body or body.strip() == "forbidden"
        return False

    def _post_ingest(self, body: dict, path: str = "/hcgi/api/ingest/disclosures") -> requests.Response:
        assert_remote_post_allowed(self.base)
        last = None
        for attempt in range(INGEST_ATTEMPTS):
            try:
                last = self.session.post(
                    f"{self.base}{path}",
                    headers={
                        "X-Ingest-Key": self.ingest_key,
                        "Content-Type": "application/json",
                    },
                    json=body,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                if not is_connection_error(exc) or attempt == INGEST_ATTEMPTS - 1:
                    raise
                delay = backoff_seconds(
                    attempt, base=INGEST_BACKOFF_BASE, cap=INGEST_BACKOFF_CAP
                )
                logger.warning(
                    "ingest connection error on %s, retry %s in %.1fs: %s",
                    path,
                    attempt + 1,
                    delay,
                    exc,
                )
                time.sleep(delay)
                continue
            if not self._should_retry(last):
                return last
            if attempt == INGEST_ATTEMPTS - 1:
                break
            delay = backoff_seconds(
                attempt, last, base=INGEST_BACKOFF_BASE, cap=INGEST_BACKOFF_CAP
            )
            logger.warning(
                "ingest HTTP %s on %s, retry %s in %.1fs",
                last.status_code,
                path,
                attempt + 1,
                delay,
            )
            time.sleep(delay)
        return last

    def fetch_judicial_judges(self) -> list[dict]:
        """Page through GET /hcgi/api/judicial/judges."""
        out: list[dict] = []
        page = 1
        while page <= 200:
            resp = self._get("/hcgi/api/judicial/judges", {"page": page, "limit": 100})
            if resp.status_code != 200:
                break
            data = resp.json()
            rows = data.get("items") or data.get("judges") or []
            if not rows:
                break
            out.extend(rows)
            total = data.get("total")
            if total is not None and len(out) >= int(total):
                break
            if len(rows) < 100:
                break
            page += 1
        return out

    def fetch_judicial_events(self) -> list[dict]:
        """Page through GET /hcgi/api/judicial/events."""
        out: list[dict] = []
        page = 1
        while page <= 200:
            resp = self._get("/hcgi/api/judicial/events", {"page": page, "limit": 100})
            if resp.status_code != 200:
                break
            data = resp.json()
            rows = data.get("items") or data.get("events") or []
            if not rows:
                break
            out.extend(rows)
            total = data.get("total")
            if total is not None and len(out) >= int(total):
                break
            if len(rows) < 100:
                break
            page += 1
        return out

    def submit_spending_awards(self, awards: list[dict]) -> dict:
        """POST a batch to /hcgi/api/ingest/spending/awards."""
        if not awards:
            return {"ok": True, "written": 0}
        for a in awards:
            if not a.get("source_url"):
                raise ValueError("source_url is required on every award")
            if a.get("jurisdiction") == "state":
                if not a.get("state_code"):
                    raise ValueError("state_code is required when jurisdiction=state")
                # State portals rarely match the federal award_type /
                # amount_type enums. Do not invent one here. Horizons must
                # accept omitted award_type/amount_type on state rows.
                if a.get("amount_type") not in (None, "obligated", "potential"):
                    raise ValueError("amount_type must be obligated, potential, or omitted")
                continue
            if a.get("amount_type") not in ("obligated", "potential"):
                raise ValueError("amount_type must be obligated or potential")
        body = {"awards": awards}
        logger.info("Submitting %d spending awards", len(awards))
        resp = self._post_ingest(body, path="/hcgi/api/ingest/spending/awards")
        try:
            data = resp.json()
        except ValueError:
            data = {"raw": resp.text[:500]}
        if resp.status_code == 404:
            logger.error(
                "Horizons route /hcgi/api/ingest/spending/awards is not deployed (404). "
                "See docs/HORIZONS_SPENDING_AWARDS.md"
            )
        if resp.status_code not in (200, 201):
            logger.error(
                "Spending ingest HTTP %s: %s",
                resp.status_code,
                str(data)[:500],
            )
            resp.raise_for_status()
        logger.info("Spending ingest OK written=%s", data.get("written") or data.get("count"))
        return data

    def submit_improper_payments(self, records: list[dict]) -> dict:
        """POST a batch to /hcgi/api/ingest/spending/improper-payments."""
        if not records:
            return {"ok": True, "written": 0}
        for rec in records:
            if not rec.get("source_url"):
                raise ValueError("source_url is required on every improper-payment record")
            if rec.get("cause_category") not in (
                "overpayment",
                "underpayment",
                "fraud",
                "documentation_error",
                "other",
            ):
                raise ValueError("invalid cause_category")
        body = {"improper_payments": records}
        logger.info("Submitting %d improper-payment records", len(records))
        resp = self._post_ingest(
            body, path="/hcgi/api/ingest/spending/improper-payments"
        )
        try:
            data = resp.json()
        except ValueError:
            data = {"raw": resp.text[:500]}
        if resp.status_code == 404:
            logger.error(
                "Horizons route /hcgi/api/ingest/spending/improper-payments "
                "is not deployed (404). See docs/HORIZONS_IMPROPER_PAYMENTS.md"
            )
        if resp.status_code not in (200, 201):
            logger.error(
                "Improper-payment ingest HTTP %s: %s",
                resp.status_code,
                str(data)[:500],
            )
            resp.raise_for_status()
        logger.info(
            "Improper-payment ingest OK written=%s",
            data.get("written") or data.get("count"),
        )
        return data

    def submit_ig_reports(self, records: list[dict]) -> dict:
        """POST a batch to /hcgi/api/ingest/spending/ig-reports."""
        if not records:
            return {"ok": True, "written": 0}
        for rec in records:
            if not rec.get("source_url"):
                raise ValueError("source_url is required on every IG report")
            if rec.get("report_type") not in (
                "audit",
                "investigation",
                "evaluation",
                "semiannual_report",
            ):
                raise ValueError("invalid report_type")
        body = {"ig_reports": records}
        logger.info("Submitting %d IG reports", len(records))
        resp = self._post_ingest(body, path="/hcgi/api/ingest/spending/ig-reports")
        try:
            data = resp.json()
        except ValueError:
            data = {"raw": resp.text[:500]}
        if resp.status_code == 404:
            logger.error(
                "Horizons route /hcgi/api/ingest/spending/ig-reports is not "
                "deployed (404). See docs/HORIZONS_IG_REPORTS.md"
            )
        if resp.status_code not in (200, 201):
            logger.error("IG ingest HTTP %s: %s", resp.status_code, str(data)[:500])
            resp.raise_for_status()
        logger.info(
            "IG ingest OK written=%s", data.get("written") or data.get("count")
        )
        return data

    def submit_bills(self, records: list[dict]) -> dict:
        """POST a batch to /hcgi/api/ingest/bills."""
        if not records:
            return {"ok": True, "written": 0}
        for rec in records:
            if not rec.get("source_url"):
                raise ValueError("source_url is required on every bill")
            if rec.get("jurisdiction") != "state":
                raise ValueError("this worker only posts jurisdiction=state")
        body = {"bills": records}
        logger.info("Submitting %d bills", len(records))
        resp = self._post_ingest(body, path="/hcgi/api/ingest/bills")
        try:
            data = resp.json()
        except ValueError:
            data = {"raw": resp.text[:500]}
        if resp.status_code == 404:
            logger.error(
                "Horizons route /hcgi/api/ingest/bills is not deployed (404). "
                "See docs/HORIZONS_LEGISCAN_BILLS.md"
            )
            return {"ok": False, "error": "route_not_found", "written": 0}
        if resp.status_code not in (200, 201):
            logger.error("Bills ingest HTTP %s: %s", resp.status_code, str(data)[:500])
            if resp.status_code >= 500 and len(records) > 1:
                written = 0
                skipped = 0
                for rec in records:
                    try:
                        self.submit_bills([rec])
                        written += 1
                    except Exception as e:
                        skipped += 1
                        logger.error(
                            "bill %s skipped after batch 500: %s",
                            rec.get("id"),
                            e,
                        )
                return {
                    "ok": skipped == 0,
                    "written": written,
                    "skipped": skipped,
                    "partial": skipped > 0,
                }
            resp.raise_for_status()
        return data

    def submit_votes(self, records: list[dict]) -> dict:
        """POST a batch to /hcgi/api/ingest/votes.

        State rows keep ``jurisdiction=state`` and a 2-letter ``state_code``.
        Federal rows match the House roll calls already stored on
        ``GET /hcgi/api/votes``: ``jurisdiction=federal``, ``state_code`` empty,
        and ``chamber`` title case (``House`` or ``Senate``).
        """
        if not records:
            return {"ok": True, "written": 0}
        for rec in records:
            if not rec.get("source_url"):
                raise ValueError("source_url is required on every vote")
            jurisdiction = rec.get("jurisdiction")
            if jurisdiction == "federal":
                if rec.get("chamber") not in ("House", "Senate"):
                    raise ValueError("federal vote chamber must be House or Senate")
                if rec.get("state_code") not in (None, ""):
                    raise ValueError("federal votes must not set state_code")
            elif jurisdiction != "state":
                raise ValueError("this worker only posts jurisdiction=state or federal")
            for mv in rec.get("member_votes") or []:
                if not mv.get("bioguide_id"):
                    raise ValueError("member vote missing bioguide_id")
        body = {"votes": records}
        logger.info("Submitting %d votes", len(records))
        resp = self._post_ingest(body, path="/hcgi/api/ingest/votes")
        try:
            data = resp.json()
        except ValueError:
            data = {"raw": resp.text[:500]}
        written = data.get("written")
        if written is None:
            written = data.get("count")
        submitted = data.get("submitted")
        if resp.status_code == 404:
            logger.error(
                "Horizons route /hcgi/api/ingest/votes is not deployed (404). "
                "See docs/HORIZONS_LEGISCAN_BILLS.md"
            )
        if resp.status_code not in (200, 201):
            logger.error(
                "Votes batch HTTP %s written=%s submitted=%s body=%s",
                resp.status_code,
                written,
                submitted,
                str(data)[:500],
            )
            resp.raise_for_status()
        logger.info(
            "Votes batch HTTP %s written=%s submitted=%s",
            resp.status_code,
            written,
            submitted,
        )
        return data

    def submit_state_coverage(self, records: list[dict]) -> dict:
        """POST /hcgi/api/ingest/state-coverage — new Horizons route."""
        if not records:
            return {"ok": True, "written": 0}
        for rec in records:
            if not rec.get("state_code"):
                raise ValueError("state_code is required on coverage rows")
        body = {"coverage": records}
        logger.info("Submitting %d state_coverage rows", len(records))
        resp = self._post_ingest(body, path="/hcgi/api/ingest/state-coverage")
        try:
            data = resp.json()
        except ValueError:
            data = {"raw": resp.text[:500]}
        if resp.status_code == 404:
            logger.error(
                "Horizons route /hcgi/api/ingest/state-coverage is not deployed "
                "(404). See docs/HORIZONS_STATE_COVERAGE.md"
            )
            return {"ok": False, "error": "route_not_found", "written": 0}
        if resp.status_code not in (200, 201):
            logger.error(
                "Coverage ingest HTTP %s: %s", resp.status_code, str(data)[:500]
            )
            resp.raise_for_status()
        return data

    def submit_members(self, records: list[dict]) -> dict:
        """POST a batch to the existing /hcgi/api/ingest/members."""
        if not records:
            return {"ok": True, "written": 0}
        for rec in records:
            if not rec.get("source_url"):
                raise ValueError("source_url is required on every member")
            if rec.get("jurisdiction") != "state":
                raise ValueError("this worker only posts jurisdiction=state")
            if rec.get("chamber") not in ("house", "senate", "unicameral"):
                raise ValueError("invalid chamber")
            if rec.get("party") not in ("D", "R", "I", "N"):
                raise ValueError("invalid party")
            if not rec.get("bioguide_id"):
                raise ValueError("bioguide_id is required")
        body = {"members": records}
        logger.info("Submitting %d members", len(records))
        resp = self._post_ingest(body, path="/hcgi/api/ingest/members")
        try:
            data = resp.json()
        except ValueError:
            data = {"raw": resp.text[:500]}
        if resp.status_code == 404:
            logger.error(
                "Horizons route /hcgi/api/ingest/members is not deployed (404). "
                "See docs/HORIZONS_LEGISCAN_MEMBERS.md"
            )
        if resp.status_code not in (200, 201):
            logger.error(
                "Members ingest HTTP %s: %s", resp.status_code, str(data)[:500]
            )
            resp.raise_for_status()
        logger.info(
            "Members ingest OK written=%s", data.get("written") or data.get("count")
        )
        return data

    def submit_public_records_offices(self, records: list[dict]) -> dict:
        """POST a batch to /hcgi/api/ingest/public-records/offices."""
        if not records:
            return {"ok": True, "written": 0}
        for rec in records:
            if not rec.get("source_url"):
                raise ValueError("source_url is required on every office")
            if rec.get("jurisdiction") not in ("federal", "state"):
                raise ValueError("invalid jurisdiction")
            if rec.get("jurisdiction") == "state" and not rec.get("state_code"):
                raise ValueError("state_code required when jurisdiction=state")
            if not rec.get("slug") or not rec.get("office_name"):
                raise ValueError("slug and office_name are required")
        body = {"offices": records}
        logger.info("Submitting %d public-records offices", len(records))
        resp = self._post_ingest(
            body, path="/hcgi/api/ingest/public-records/offices"
        )
        try:
            data = resp.json()
        except ValueError:
            data = {"raw": resp.text[:500]}
        if resp.status_code not in (200, 201):
            logger.error(
                "Offices ingest HTTP %s: %s", resp.status_code, str(data)[:500]
            )
            if resp.status_code in (400, 500) and len(records) > 1:
                written = 0
                skipped = 0
                for rec in records:
                    try:
                        self.submit_public_records_offices([rec])
                        written += 1
                    except Exception as e:
                        skipped += 1
                        logger.error(
                            "office %s skipped: %s", rec.get("slug"), e
                        )
                return {
                    "ok": skipped == 0,
                    "written": written,
                    "skipped": skipped,
                    "partial": True,
                }
            resp.raise_for_status()
        logger.info(
            "Offices ingest OK written=%s", data.get("written") or data.get("count")
        )
        return data

    def submit_public_records_jurisdictions(self, records: list[dict]) -> dict:
        """POST a batch to /hcgi/api/ingest/public-records/jurisdictions."""
        if not records:
            return {"ok": True, "written": 0}
        for rec in records:
            if not rec.get("source_url") or not rec.get("portal_url"):
                raise ValueError("source_url and portal_url are required")
            if rec.get("jurisdiction") not in ("federal", "state"):
                raise ValueError("invalid jurisdiction")
            if rec.get("jurisdiction") == "state" and not rec.get("state_code"):
                raise ValueError("state_code required when jurisdiction=state")
        body = {"jurisdictions": records}
        logger.info("Submitting %d public-records jurisdictions", len(records))
        resp = self._post_ingest(
            body, path="/hcgi/api/ingest/public-records/jurisdictions"
        )
        try:
            data = resp.json()
        except ValueError:
            data = {"raw": resp.text[:500]}
        if resp.status_code not in (200, 201):
            logger.error(
                "Jurisdictions ingest HTTP %s: %s", resp.status_code, str(data)[:500]
            )
            resp.raise_for_status()
        return data

    def submit_elections_jurisdictions(self, records: list[dict]) -> dict:
        """POST a batch to /hcgi/api/ingest/elections/jurisdictions."""
        if not records:
            return {"ok": True, "written": 0}
        for rec in records:
            if not rec.get("source_url") or not rec.get("sos_portal_url"):
                raise ValueError("source_url and sos_portal_url are required")
            if rec.get("jurisdiction") not in ("federal", "state"):
                raise ValueError("invalid jurisdiction")
            if rec.get("jurisdiction") == "state" and not rec.get("state_code"):
                raise ValueError("state_code required when jurisdiction=state")
        body = {"jurisdictions": records}
        logger.info("Submitting %d elections jurisdictions", len(records))
        resp = self._post_ingest(
            body, path="/hcgi/api/ingest/elections/jurisdictions"
        )
        try:
            data = resp.json()
        except ValueError:
            data = {"raw": resp.text[:500]}
        if resp.status_code not in (200, 201):
            logger.error(
                "Elections jurisdictions HTTP %s: %s",
                resp.status_code,
                str(data)[:500],
            )
            if resp.status_code in (400, 500) and len(records) > 1:
                written = 0
                skipped = 0
                for rec in records:
                    try:
                        self.submit_elections_jurisdictions([rec])
                        written += 1
                    except Exception as e:
                        skipped += 1
                        logger.error(
                            "elections jurisdiction %s skipped: %s",
                            rec.get("state_code") or rec.get("name"),
                            e,
                        )
                return {
                    "ok": skipped == 0,
                    "written": written,
                    "skipped": skipped,
                    "partial": True,
                }
            resp.raise_for_status()
        return data

    def submit_elections_races(self, records: list[dict]) -> dict:
        """POST a batch to /hcgi/api/ingest/elections/races."""
        if not records:
            return {"ok": True, "written": 0}
        for rec in records:
            if not rec.get("source_url"):
                raise ValueError("source_url is required on every race")
            if not rec.get("slug") or not rec.get("office_name"):
                raise ValueError("slug and office_name are required")
            if rec.get("jurisdiction") not in ("federal", "state"):
                raise ValueError("invalid jurisdiction")
            if rec.get("jurisdiction") == "state" and not rec.get("state_code"):
                raise ValueError("state_code required when jurisdiction=state")
        body = {"races": records}
        logger.info("Submitting %d elections races", len(records))
        resp = self._post_ingest(body, path="/hcgi/api/ingest/elections/races")
        try:
            data = resp.json()
        except ValueError:
            data = {"raw": resp.text[:500]}
        if resp.status_code not in (200, 201):
            logger.error(
                "Elections races HTTP %s: %s", resp.status_code, str(data)[:500]
            )
            if resp.status_code in (400, 500) and len(records) > 1:
                written = 0
                skipped = 0
                for rec in records:
                    try:
                        self.submit_elections_races([rec])
                        written += 1
                    except Exception as e:
                        skipped += 1
                        logger.error("race %s skipped: %s", rec.get("slug"), e)
                return {
                    "ok": skipped == 0,
                    "written": written,
                    "skipped": skipped,
                    "partial": True,
                }
            resp.raise_for_status()
        return data

    def submit_elections_candidates(self, records: list[dict]) -> dict:
        """POST a batch to /hcgi/api/ingest/elections/candidates."""
        if not records:
            return {"ok": True, "written": 0}
        for rec in records:
            if not rec.get("source_url"):
                raise ValueError("source_url is required on every candidate")
            if not rec.get("race_slug") or not rec.get("fec_candidate_id"):
                raise ValueError("race_slug and fec_candidate_id are required")
            if rec.get("party") not in ("D", "R", "I", "N", "O"):
                raise ValueError("invalid party")
        body = {"candidates": records}
        logger.info("Submitting %d elections candidates", len(records))
        resp = self._post_ingest(
            body, path="/hcgi/api/ingest/elections/candidates"
        )
        try:
            data = resp.json()
        except ValueError:
            data = {"raw": resp.text[:500]}
        if resp.status_code not in (200, 201):
            logger.error(
                "Elections candidates HTTP %s: %s",
                resp.status_code,
                str(data)[:500],
            )
            if resp.status_code in (400, 500) and len(records) > 1:
                written = 0
                skipped = 0
                for rec in records:
                    try:
                        self.submit_elections_candidates([rec])
                        written += 1
                    except Exception as e:
                        skipped += 1
                        logger.error(
                            "candidate %s skipped: %s",
                            rec.get("fec_candidate_id"),
                            e,
                        )
                return {
                    "ok": skipped == 0,
                    "written": written,
                    "skipped": skipped,
                    "partial": True,
                }
            resp.raise_for_status()
        return data

    def fetch_members(
        self,
        *,
        jurisdiction: str | None = None,
        page_size: int = 100,
    ) -> list[dict]:
        """Page through GET /hcgi/api/members."""
        out: list[dict] = []
        page = 1
        while page <= 200:
            params: dict[str, Any] = {"page": page, "limit": page_size}
            if jurisdiction:
                params["jurisdiction"] = jurisdiction
            resp = self._get("/hcgi/api/members", params)
            if resp.status_code != 200:
                break
            data = resp.json()
            rows = data.get("items") or data.get("members") or []
            if not rows:
                break
            out.extend(rows)
            total = data.get("total")
            if total is not None and len(out) >= int(total):
                break
            if len(rows) < page_size:
                break
            page += 1
        return out

    def submit_judicial_nominations(self, records: list[dict]) -> dict:
        """POST a batch to /hcgi/api/ingest/judicial/nominations."""
        if not records:
            return {"ok": True, "written": 0}
        results = ("confirmed", "rejected", "withdrawn", "returned")
        for rec in records:
            if not rec.get("source_url"):
                raise ValueError("source_url is required on every nomination")
            if not rec.get("nomination_id"):
                raise ValueError("nomination_id is required")
            if rec.get("vote_result") not in results:
                raise ValueError("invalid vote_result")
        body = {"nominations": records}
        logger.info("Submitting %d judicial nominations", len(records))
        resp = self._post_ingest(
            body, path="/hcgi/api/ingest/judicial/nominations"
        )
        try:
            data = resp.json()
        except ValueError:
            data = {"raw": resp.text[:500]}
        if resp.status_code == 404:
            logger.error(
                "Horizons route /hcgi/api/ingest/judicial/nominations is not "
                "deployed (404). See docs/HORIZONS_JUDICIAL_NOMINATIONS.md"
            )
        if resp.status_code not in (200, 201):
            logger.error(
                "Judicial-nomination ingest HTTP %s: %s",
                resp.status_code,
                str(data)[:500],
            )
            resp.raise_for_status()
        logger.info(
            "Judicial-nomination ingest OK written=%s",
            data.get("written") or data.get("count"),
        )
        return data

    def submit_judicial_disclosures(self, records: list[dict]) -> dict:
        """POST a batch to /hcgi/api/ingest/judicial/disclosures."""
        if not records:
            return {"ok": True, "written": 0}
        filing_types = ("annual", "periodic_transaction")
        txn_types = ("purchase", "sale", "exchange")
        for rec in records:
            if not rec.get("source_url"):
                raise ValueError("source_url is required on every filing")
            if not rec.get("judge_id"):
                raise ValueError("judge_id is required on every filing")
            if not rec.get("filing_id"):
                raise ValueError("filing_id is required on every filing")
            if rec.get("filing_type") not in filing_types:
                raise ValueError("invalid filing_type")
            for txn in rec.get("transactions") or []:
                if not txn.get("source_url"):
                    raise ValueError("source_url is required on every transaction")
                if txn.get("transaction_type") not in txn_types:
                    raise ValueError("invalid transaction_type")
            for asset in rec.get("annual_assets") or []:
                if not asset.get("source_url"):
                    raise ValueError("source_url is required on every annual asset")
        body = {"filings": records}
        logger.info("Submitting %d judicial disclosures", len(records))
        resp = self._post_ingest(
            body, path="/hcgi/api/ingest/judicial/disclosures"
        )
        try:
            data = resp.json()
        except ValueError:
            data = {"raw": resp.text[:500]}
        if resp.status_code == 404:
            logger.error(
                "Horizons route /hcgi/api/ingest/judicial/disclosures is not "
                "deployed (404). See docs/HORIZONS_JUDICIAL_DISCLOSURES.md"
            )
        if resp.status_code not in (200, 201):
            logger.error(
                "Judicial-disclosure ingest HTTP %s: %s",
                resp.status_code,
                str(data)[:500],
            )
            resp.raise_for_status()
        logger.info(
            "Judicial-disclosure ingest OK written=%s",
            data.get("written") or data.get("count"),
        )
        return data

    def submit_judicial_judges(self, records: list[dict]) -> dict:
        """POST a batch to /hcgi/api/ingest/judicial/judges."""
        if not records:
            return {"ok": True, "written": 0}
        court_types = (
            "supreme_court",
            "circuit_court",
            "district_court",
            "bankruptcy_court",
            "magistrate",
            "other",
        )
        statuses = (
            "active",
            "senior_status",
            "retired",
            "resigned",
            "deceased",
        )
        for rec in records:
            if not rec.get("source_url"):
                raise ValueError("source_url is required on every judge")
            if not rec.get("judge_id"):
                raise ValueError("judge_id is required on every judge")
            if rec.get("court_type") not in court_types:
                raise ValueError("invalid court_type")
            if rec.get("status") not in statuses:
                raise ValueError("invalid status")
        body = {"judges": records}
        logger.info("Submitting %d judges", len(records))
        resp = self._post_ingest(
            body, path="/hcgi/api/ingest/judicial/judges"
        )
        try:
            data = resp.json()
        except ValueError:
            data = {"raw": resp.text[:500]}
        if resp.status_code == 404:
            logger.error(
                "Horizons route /hcgi/api/ingest/judicial/judges is not "
                "deployed (404). See docs/HORIZONS_JUDICIAL_JUDGES.md"
            )
        if resp.status_code not in (200, 201):
            logger.error(
                "Judicial-judge ingest HTTP %s: %s",
                resp.status_code,
                str(data)[:500],
            )
            resp.raise_for_status()
        logger.info(
            "Judicial-judge ingest OK written=%s",
            data.get("written") or data.get("count"),
        )
        return data

    def submit_judicial_events(self, records: list[dict]) -> dict:
        """POST a batch to /hcgi/api/ingest/judicial/events."""
        from .judicial_events import CLASSIFICATIONS, EVENT_TYPES

        if not records:
            return {"ok": True, "written": 0}
        for rec in records:
            if not rec.get("source_url"):
                raise ValueError("source_url is required on every event")
            if not rec.get("judge_id"):
                raise ValueError("judge_id is required on every event")
            if not rec.get("event_id"):
                raise ValueError("event_id is required on every event")
            if rec.get("event_type") not in EVENT_TYPES:
                raise ValueError("invalid event_type")
            if rec.get("classification") not in CLASSIFICATIONS:
                raise ValueError("invalid classification")
            if not rec.get("event_date"):
                raise ValueError("event_date is required on every event")
            if not rec.get("court"):
                raise ValueError("court is required on every event")
            weight = rec.get("weight")
            if weight is None or not (1 <= float(weight) <= 5):
                raise ValueError("weight must be 1-5")
        body = {"events": records}
        logger.info("Submitting %d judicial events", len(records))
        resp = self._post_ingest(body, path="/hcgi/api/ingest/judicial/events")
        try:
            data = resp.json()
        except ValueError:
            data = {"raw": resp.text[:500]}
        if resp.status_code == 404:
            logger.error(
                "Horizons route /hcgi/api/ingest/judicial/events is not "
                "deployed (404). See docs/HORIZONS_JUDICIAL_SCORES.md"
            )
        if resp.status_code not in (200, 201):
            logger.error(
                "Judicial-event ingest HTTP %s: %s",
                resp.status_code,
                str(data)[:500],
            )
            resp.raise_for_status()
        logger.info(
            "Judicial-event ingest OK written=%s",
            data.get("written") or data.get("count"),
        )
        return data

    def submit_judicial_scores(self, records: list[dict]) -> dict:
        """POST a batch to /hcgi/api/ingest/judicial/scores."""
        bands = ("unscored", "within_bounds", "watch", "rogue")
        if not records:
            return {"ok": True, "written": 0}
        for rec in records:
            if not rec.get("judge_id"):
                raise ValueError("judge_id is required on every score")
            if rec.get("rogue_score") is None:
                raise ValueError("rogue_score is required")
            if rec.get("rogue_band") not in bands:
                raise ValueError("invalid rogue_band")
            if rec.get("event_count") is None:
                raise ValueError("event_count is required")
            if not rec.get("scored_through"):
                raise ValueError("scored_through is required")
        body = {"scores": records}
        logger.info("Submitting %d judicial scores", len(records))
        resp = self._post_ingest(body, path="/hcgi/api/ingest/judicial/scores")
        try:
            data = resp.json()
        except ValueError:
            data = {"raw": resp.text[:500]}
        if resp.status_code == 404:
            logger.error(
                "Horizons route /hcgi/api/ingest/judicial/scores is not "
                "deployed (404). See docs/HORIZONS_JUDICIAL_SCORES.md"
            )
        if resp.status_code not in (200, 201):
            logger.error(
                "Judicial-score ingest HTTP %s: %s",
                resp.status_code,
                str(data)[:500],
            )
            resp.raise_for_status()
        logger.info(
            "Judicial-score ingest OK written=%s",
            data.get("written") or data.get("count"),
        )
        return data

    def submit_fraud_cases(self, records: list[dict]) -> dict:
        """POST a batch to /hcgi/api/ingest/spending/fraud-cases."""
        if not records:
            return {"ok": True, "written": 0}
        for rec in records:
            if not rec.get("source_url"):
                raise ValueError("source_url is required on every fraud case")
            if rec.get("status") not in (
                "charged",
                "convicted",
                "settled",
                "ongoing",
            ):
                raise ValueError("invalid status")
            if rec.get("defendant_type") not in (
                "individual",
                "company",
                "multiple",
            ):
                raise ValueError("invalid defendant_type")
            state = rec.get("state_code")
            if state is not None and state != "":
                if not (
                    isinstance(state, str)
                    and len(state) == 2
                    and state.isalpha()
                ):
                    raise ValueError("state_code must be a 2-letter USPS code")
                rec["state_code"] = state.upper()
        body = {"fraud_cases": records}
        logger.info("Submitting %d fraud cases", len(records))
        resp = self._post_ingest(
            body, path="/hcgi/api/ingest/spending/fraud-cases"
        )
        try:
            data = resp.json()
        except ValueError:
            data = {"raw": resp.text[:500]}
        if resp.status_code == 404:
            logger.error(
                "Horizons route /hcgi/api/ingest/spending/fraud-cases is not "
                "deployed (404). See docs/HORIZONS_FRAUD_CASES.md"
            )
        if resp.status_code not in (200, 201):
            logger.error(
                "Fraud-case ingest HTTP %s: %s", resp.status_code, str(data)[:500]
            )
            resp.raise_for_status()
        logger.info(
            "Fraud-case ingest OK written=%s",
            data.get("written") or data.get("count"),
        )
        return data

    def list_disclosure_filings(self) -> list[dict]:
        """Every stored disclosure filing, with that filing's transactions nested.

        Public reads are ``GET /hcgi/api/members`` (paged) and
        ``GET /hcgi/api/members/{id}/disclosures``. ``/hcgi/api/filings`` and
        ``/hcgi/api/disclosures/filings`` are not deployed. Raises
        ``DisclosureListError`` when those reads do not answer.
        """
        filings, _transactions = self._disclosure_catalog()
        return filings

    def list_disclosure_transactions(self) -> list[dict]:
        """Transactions belonging to the filings from ``list_disclosure_filings``."""
        _filings, transactions = self._disclosure_catalog()
        return transactions

    def _disclosure_catalog(self) -> tuple[list[dict], list[dict]]:
        cached = getattr(self, "_disclosure_catalog_cache", None)
        if cached is not None:
            return cached
        catalog = self._fetch_disclosure_catalog()
        self._disclosure_catalog_cache = catalog
        return catalog

    def _fetch_disclosure_catalog(self) -> tuple[list[dict], list[dict]]:
        # The public API allows 100 reads per 300 seconds. `_get` waits out
        # `ratelimit-reset` before the window is spent, so a 429 does not
        # turn into a partial filing list.
        members = self._list_federal_members()
        filings_by_id: dict[str, dict] = {}
        transactions: list[dict] = []
        seen_tx: set[tuple[str, str, str]] = set()
        disclosures_ok = 0
        for member in members:
            member_key = str(member.get("id") or member.get("bioguide_id") or "").strip()
            if not member_key:
                continue
            path = f"/hcgi/api/members/{quote(member_key, safe='')}/disclosures"
            try:
                resp = self._get_ok(path)
            except Exception as exc:
                raise DisclosureListError(
                    f"GET {path} failed: {exc}. Refusing a partial filing list."
                ) from exc
            if resp.status_code == 404:
                continue
            if resp.status_code != 200:
                raise DisclosureListError(
                    f"GET {path} HTTP {resp.status_code}. "
                    "Refusing a partial filing list."
                )
            try:
                data = resp.json()
            except ValueError as exc:
                raise DisclosureListError(
                    f"GET {path} did not return JSON. Refusing a partial filing list."
                ) from exc
            disclosures_ok += 1
            if disclosures_ok % 50 == 0 or disclosures_ok == 1:
                logger.info(
                    "disclosure list progress members=%d filings=%d remaining=%s",
                    disclosures_ok,
                    len(filings_by_id),
                    getattr(self, "_ratelimit_remaining", None),
                )
            member_filings = [
                _without_server_meta(row)
                for row in (data.get("filings") or [])
                if isinstance(row, dict)
            ]
            member_txns = [
                _without_server_meta(row)
                for row in (data.get("transactions") or [])
                if isinstance(row, dict)
            ]
            grouped: dict[str, list[dict]] = {}
            for txn in member_txns:
                filing_id = str(txn.get("filing_id") or "").strip()
                identity = (
                    filing_id,
                    str(txn.get("id") or ""),
                    str(txn.get("line_index")),
                )
                if identity in seen_tx:
                    continue
                seen_tx.add(identity)
                transactions.append(txn)
                grouped.setdefault(filing_id, []).append(txn)
            for filing in member_filings:
                filing_id = str(filing.get("id") or "").strip()
                owned = grouped.get(filing_id, [])
                owned.sort(key=lambda row: (row.get("line_index") is None, row.get("line_index") or 0))
                if filing_id and filing_id in filings_by_id:
                    existing = filings_by_id[filing_id]
                    have = {
                        (
                            str(row.get("id") or ""),
                            str(row.get("line_index")),
                        )
                        for row in existing.get("transactions") or []
                    }
                    for txn in owned:
                        key = (str(txn.get("id") or ""), str(txn.get("line_index")))
                        if key not in have:
                            existing.setdefault("transactions", []).append(txn)
                    continue
                filing = dict(filing)
                filing["transactions"] = owned
                key = filing_id or str(filing.get("source_url") or len(filings_by_id))
                filings_by_id[key] = filing
        if disclosures_ok == 0:
            raise DisclosureListError(
                "GET /hcgi/api/members/{id}/disclosures did not return a filing list. "
                "GET /hcgi/api/filings, /hcgi/api/disclosures/filings, and "
                "/hcgi/api/transactions are not deployed."
            )
        filings = list(filings_by_id.values())
        house = sum(1 for row in filings if str(row.get("chamber") or "").lower() == "house")
        senate = sum(1 for row in filings if str(row.get("chamber") or "").lower() == "senate")
        logger.info(
            "Listed %d filings (%d House, %d Senate) and %d transactions "
            "from %d members via /hcgi/api/members/{id}/disclosures",
            len(filings),
            house,
            senate,
            len(transactions),
            disclosures_ok,
        )
        return filings, transactions

    def _list_federal_members(self) -> list[dict]:
        """Page GET /hcgi/api/members?jurisdiction=federal."""
        out: list[dict] = []
        page = 1
        limit = 100
        while page <= 30:
            path = "/hcgi/api/members"
            try:
                resp = self._get_ok(
                    path,
                    {"page": page, "limit": limit, "jurisdiction": "federal"},
                )
            except requests.RequestException as exc:
                raise DisclosureListError(
                    f"GET {path} failed: {exc}"
                ) from exc
            if resp.status_code == 429:
                raise DisclosureListError(
                    f"GET {path} stayed rate-limited (HTTP 429)"
                )
            if resp.status_code == 404:
                raise DisclosureListError(
                    "GET /hcgi/api/members returned 404. "
                    "Disclosure filings are not listed on /hcgi/api/filings, "
                    "/hcgi/api/disclosures, or /hcgi/api/disclosures/filings."
                )
            if resp.status_code != 200:
                raise DisclosureListError(
                    f"GET {path} HTTP {resp.status_code}"
                )
            try:
                data = resp.json()
            except ValueError as exc:
                raise DisclosureListError(
                    f"GET {path} did not return JSON"
                ) from exc
            rows = data.get("items") or data.get("members") or []
            if not isinstance(rows, list):
                raise DisclosureListError(f"GET {path} items were not a list")
            out.extend(row for row in rows if isinstance(row, dict))
            if not rows:
                break
            total = data.get("total")
            total_pages = data.get("totalPages")
            if total is not None and len(out) >= int(total):
                break
            if total_pages is not None and page >= int(total_pages):
                break
            if total is None and total_pages is None and len(rows) < limit:
                break
            page += 1
        return out

    def _get_ok(self, path: str, params: dict | None = None, attempts: int = 8):
        """GET, backing off on rate limits and transient gateway errors.

        HTTP 429 waits for ``ratelimit-reset`` (applied on the next ``_get``)
        instead of sending another request into the same window.
        """
        delay = 3.0
        last = None
        for attempt in range(attempts):
            try:
                last = self._get(path, params)
            except Exception as exc:
                if attempt == attempts - 1:
                    raise
                logger.warning("list %s error %s, retry in %ss", path, exc, delay)
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            if last.status_code not in _LIST_RETRY_STATUS:
                return last
            # A few full windows are enough. More retries would sit here for
            # tens of minutes and still not be a complete catalog.
            if last.status_code == 429 and attempt >= 3:
                break
            if attempt == attempts - 1:
                break
            if last.status_code == 429 and getattr(self, "_ratelimit_reset", None):
                logger.warning("list %s HTTP 429, waiting for ratelimit-reset", path)
                continue
            logger.warning(
                "list %s HTTP %s, retry in %ss", path, last.status_code, delay
            )
            time.sleep(delay)
            delay = min(delay * 2, 30)
        return last

    def lookup_member_by_name(
        self,
        name: str,
        state: str | None = None,
        district: int | None = None,
        office: str | None = None,
        bioguide_id: str | None = None,
        state_district: str | None = None,
        chamber: str | None = None,
    ) -> dict | None:
        """Resolve a filer to a Horizons members row.

        Horizons stores full state names. Filings often have a USPS code.
        Try the normalized full name plus that state, then the last name
        plus state, then the bioguide id when the filing or the
        congress-legislators roster knows one. A roster hit is not itself
        a member id — the writer drops unknown ids and the trades stay
        unlinked.
        """
        if state_district and (state is None or district is None):
            sc, dist = parse_state_district(state_district)
            state = state or sc
            if district is None:
                district = dist
        # eFD office lines sometimes carry the state the search row omitted.
        if not state:
            office_state = state_from_office(office)
            if office_state:
                state = office_state

        local = match_legislator(
            name,
            state=state,
            district=district,
            chamber=chamber,
            office=office,
            state_district=state_district,
        )
        state_label = state_for_member_lookup(state)
        if not state_label and local and local.get("state"):
            state_label = state_for_member_lookup(local.get("state"))
        full_name, last_name = normalized_lookup_names(name, office=office)
        known_bioguide = (bioguide_id or "").strip() or str((local or {}).get("bioguide") or "").strip()
        lookup_names = _lookup_name_candidates(full_name, local)

        def accept_name_hit(candidate: str, member: dict | None) -> dict | None:
            if not member:
                logger.info("Member lookup MISS name=%r state=%r", candidate, state_label)
                return None
            if not member_record_compatible(name, member, state=state, office=office):
                logger.info(
                    "Member lookup REJECT name=%r hit=%r bioguide=%s",
                    candidate,
                    member.get("name"),
                    member.get("bioguide_id"),
                )
                return None
            logger.info(
                "Member lookup HIT name=%r state=%r → id=%s bioguide=%s stored_name=%r",
                candidate,
                state_label,
                member.get("id"),
                member.get("bioguide_id"),
                member.get("name"),
            )
            return _with_roster_party(member, local)

        rate_limited = False
        bioguide_answered = False

        def lookup_name(candidate: str) -> dict | None:
            nonlocal rate_limited
            logger.info(
                "Member lookup attempt name=%r state=%r", candidate, state_label
            )
            try:
                member = self._lookup_http(
                    name=candidate, state=state_label, chamber=chamber
                )
            except LookupRateLimited:
                # Stop the remaining name variants. A 429 is not a miss.
                logger.warning(
                    "Member lookup rate-limited name=%r state=%r; not a miss",
                    candidate,
                    state_label,
                )
                rate_limited = True
                return None
            hit = accept_name_hit(candidate, member)
            if hit and _row_id_is_bioguide(hit):
                logger.info(
                    "Member lookup name=%r id=%s is a bioguide; "
                    "continuing to the database id",
                    candidate,
                    hit.get("id"),
                )
                return None
            return hit

        for candidate in lookup_names:
            hit = lookup_name(candidate)
            if hit:
                return hit
            if rate_limited:
                break

        if (
            not rate_limited
            and last_name
            and last_name.lower() not in {n.lower() for n in lookup_names}
        ):
            hit = lookup_name(last_name)
            if hit:
                return hit

        if known_bioguide:
            logger.info("Member lookup attempt bioguide_id=%s", known_bioguide)
            try:
                member = self._lookup_http(
                    bioguide_id=known_bioguide, chamber=chamber
                )
            except LookupRateLimited:
                logger.warning(
                    "Member lookup rate-limited bioguide_id=%s; not a miss",
                    known_bioguide,
                )
                rate_limited = True
            else:
                bioguide_answered = True
                returned = str((member or {}).get("bioguide_id") or "")
                if member and returned == known_bioguide:
                    logger.info(
                        "Member lookup bioguide_id=%s → %s (%s)",
                        known_bioguide,
                        member.get("id"),
                        member.get("name"),
                    )
                    return _with_roster_party(member, local)
                logger.info("Member lookup MISS bioguide_id=%s", known_bioguide)

        if rate_limited and not bioguide_answered:
            raise LookupRateLimited(
                f"member lookup rate-limited for name={name!r} "
                f"bioguide={known_bioguide or ''}"
            )

        logger.warning(
            "Member lookup FAILED for name=%r state=%s district=%s office=%r "
            "full_name=%r last_name=%r bioguide=%s",
            name,
            state_label or state,
            district,
            office,
            full_name,
            last_name,
            known_bioguide,
        )
        return None

    def _throttle_lookup(self) -> None:
        now = time.monotonic()
        last = getattr(self, "_last_lookup_at", 0.0)
        wait = LOOKUP_MIN_INTERVAL - (now - last)
        if last and wait > 0:
            time.sleep(wait)
            now = time.monotonic()
        self._last_lookup_at = now

    def _lookup_http(self, **params) -> dict | None:
        # drop Nones. Chamber is part of the cache key only — the lookup
        # route does not take it as a query param.
        params = {k: v for k, v in params.items() if v is not None and v != ""}
        if not params:
            return None
        key = _member_lookup_cache_key(params)
        if key in self._lookup_cache:
            cached = self._lookup_cache[key]
            return dict(cached) if cached else None
        query = {k: v for k, v in params.items() if k != "chamber"}
        if not query:
            return None
        self._throttle_lookup()
        resp = None
        for attempt in range(LOOKUP_ATTEMPTS):
            try:
                resp = self._get("/hcgi/api/members/lookup", query)
            except requests.RequestException as exc:
                if not is_connection_error(exc) or attempt == LOOKUP_ATTEMPTS - 1:
                    logger.error("lookup request failed: %s", exc)
                    return None
                delay = backoff_seconds(
                    attempt, base=LOOKUP_BACKOFF_BASE, cap=LOOKUP_BACKOFF_CAP
                )
                logger.warning(
                    "lookup connection error, retry in %.1fs: %s", delay, exc
                )
                time.sleep(delay)
                continue
            if resp.status_code != 429:
                break
            if attempt == LOOKUP_ATTEMPTS - 1:
                body = (resp.text or "")[:200]
                logger.warning("lookup HTTP 429: %s", body)
                raise LookupRateLimited(
                    f"lookup HTTP 429 after {LOOKUP_ATTEMPTS} attempts: {body}"
                )
            delay = backoff_seconds(
                attempt, resp, base=LOOKUP_BACKOFF_BASE, cap=LOOKUP_BACKOFF_CAP
            )
            logger.warning("lookup HTTP 429, retry in %.1fs", delay)
            time.sleep(delay)
        if resp is None:
            return None
        if resp.status_code != 200:
            logger.warning("lookup HTTP %s: %s", resp.status_code, resp.text[:200])
            # 5xx and 429 are not a definitive miss. 429 already raised.
            if resp.status_code < 500:
                self._lookup_cache[key] = None
            return None
        try:
            data = resp.json()
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        member = None
        if data.get("found"):
            member = data.get("member")
            if not member:
                members = data.get("members") or []
                member = members[0] if members else None
        if not isinstance(member, dict):
            self._lookup_cache[key] = None
            return None
        stored = dict(member)
        self._lookup_cache[key] = stored
        return dict(stored)

    def _bioguide_from_congress(
        self,
        parsed,
        state: str | None = None,
        district: int | None = None,
    ) -> str | None:
        try:
            resp = self._get("/hcgi/api/congress/members")
            resp.raise_for_status()
            roster = resp.json().get("members") or []
        except Exception as e:
            logger.warning("congress members fetch failed: %s", e)
            return None

        last = (parsed.last or "").lower()
        first = (parsed.first or "").lower()
        if not last:
            return None

        hits = []
        for m in roster:
            n = (m.get("name") or "").lower()
            # roster may be "Last, First" or "First Last"
            if last not in n:
                continue
            if first and first[:3] not in n and first not in n:
                continue
            if state:
                ms = (m.get("state") or "").upper()
                # state may be "MD" or "Maryland"
                if len(state) == 2:
                    if ms != state.upper() and not ms.startswith(state.upper()):
                        # compare abbreviation loosely
                        from .names import normalize_state

                        if normalize_state(m.get("state")) != state.upper():
                            continue
                elif state.lower() not in (m.get("state") or "").lower():
                    continue
            if district is not None and m.get("district") not in (None, "", 0):
                try:
                    if int(m["district"]) != int(district):
                        continue
                except (TypeError, ValueError):
                    pass
            hits.append(m)

        if len(hits) == 1:
            return hits[0].get("id") or hits[0].get("bioguide_id")
        if len(hits) > 1:
            logger.warning(
                "Ambiguous congress roster hits for %s %s: %s",
                first,
                last,
                [(h.get("id"), h.get("name")) for h in hits[:5]],
            )
        return None

    def submit_filing(
        self,
        filing: dict,
        transactions: list[dict] | None = None,
        annual_assets: list[dict] | None = None,
        ignore_stale_date: bool = False,
    ) -> dict:
        """POST type=filing with nested payload. Returns API JSON body.

        ``ignore_stale_date`` is for the member backfill, which relinks
        filings already stored outside the one-year ingest window.
        """
        if not filing.get("source_url"):
            raise ValueError("source_url is required on every filing")
        guard_filing = filing
        if ignore_stale_date:
            guard_filing = {
                key: value for key, value in filing.items() if key != "filing_date"
            }
        if skip_reason(guard_filing, kind="filing", require_name=True):
            return {
                "ok": False,
                "skipped": True,
                "transactions_written": 0,
            }

        prepared: list[dict] = []
        for i, txn in enumerate(transactions or []):
            row = dict(txn)
            # Keep parser-assigned positions. Fill any gap so the writer
            # can tell identical trades apart.
            if row.get("line_index") is None:
                row["line_index"] = i
            if skip_reason(row, kind="transaction", require_name=False):
                continue
            prepared.append(row)
        txns = [clean_transaction(t) for t in prepared]
        filing_out = {
            key: value
            for key, value in filing.items()
            if key not in ("member_id", "member_party") or str(value or "").strip()
        }
        body = {
            "type": "filing",
            "payload": {
                "filing": filing_out,
                "transactions": txns,
                "annual_assets": annual_assets or [],
            },
        }
        logger.info(
            "Submitting filing source_url=%s member_id=%s bioguide=%s txns=%d",
            filing_out.get("source_url"),
            filing_out.get("member_id"),
            filing_out.get("bioguide_id"),
            len(txns),
        )
        resp = self._post_ingest(body)
        # Some member ids 500 the Horizons writer while the same payload
        # without member_id is accepted. Keep party, name, and bioguide so
        # the filing still lands instead of dropping the PTR.
        if resp.status_code == 500 and filing_out.get("member_id"):
            logger.warning(
                "Ingest HTTP 500 for source_url=%s member_id=%s; "
                "retrying without member_id",
                filing_out.get("source_url"),
                filing_out.get("member_id"),
            )
            retry_filing = dict(filing_out)
            retry_filing.pop("member_id", None)
            body["payload"]["filing"] = retry_filing
            resp = self._post_ingest(body)
        try:
            data = resp.json()
        except ValueError:
            data = {"raw": resp.text[:500]}
        if resp.status_code not in (200, 201):
            logger.error(
                "Ingest submit failed HTTP %s: %s",
                resp.status_code,
                str(data)[:500],
            )
            resp.raise_for_status()
        logger.info(
            "Ingest OK filing_id=%s transactions_written=%s",
            data.get("filing_id") or data.get("id"),
            data.get("transactions_written"),
        )
        return data

    def submit_ingestion_log(
        self,
        source: str,
        found: int,
        parsed: int,
        failed: int,
        flagged: int = 0,
        note: str = "",
        duration_seconds: float | None = None,
    ) -> dict | None:
        """Best-effort run log. Ignore failures so scrape results still count."""
        from datetime import datetime, timezone

        payload = {
            "source": source
            if source
            in (
                "house_clerk",
                "senate_efd",
                "congress_gov",
                "usaspending",
                "gao",
                "oversight",
                "prac",
                "fjc",
                "jefs",
                "nominations",
                "legiscan",
                "ca_spending",
                "senate_votes",
            )
            else (
                "house_clerk"
                if "house" in source
                else "senate_efd"
                if "senate" in source
                else "usaspending"
                if "spend" in source
                else "gao"
                if "gao" in source
                else "oversight"
                if "oversight" in source or source == "ig"
                else "prac"
                if "prac" in source or "fraud" in source
                else "fjc"
                if "fjc" in source or "judge" in source
                else "jefs"
                if "jefs" in source
                else "nominations"
                if "nominat" in source
                else "legiscan"
                if "legiscan" in source or "state" in source
                else "congress_gov"
            ),
            "run_timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "filings_found": found,
            "filings_parsed_success": parsed,
            "filings_failed": failed,
            "filings_flagged_for_review": flagged,
            "error_message": note or "",
        }
        if duration_seconds is not None:
            payload["duration_seconds"] = duration_seconds
        try:
            resp = self._post_ingest({"type": "ingestion_log", "payload": payload})
            if resp.status_code in (200, 201):
                return resp.json()
            if 500 <= resp.status_code < 600:
                logger.warning(
                    "ingestion_log submit HTTP %s; response body: %s; retrying once",
                    resp.status_code,
                    _ingestion_response_body(resp),
                )
                resp = self._post_ingest({"type": "ingestion_log", "payload": payload})
                if resp.status_code in (200, 201):
                    return resp.json()
            logger.warning(
                "ingestion_log submit HTTP %s; response body: %s",
                resp.status_code,
                _ingestion_response_body(resp),
            )
        except Exception as e:
            logger.warning("ingestion_log submit failed: %s", e)
        return None

    def flag_manual_review(
        self,
        filing_id: str,
        reason: str = "unexpected_structure",
        notes: str = "",
    ) -> dict | None:
        payload = {
            "filing_id": filing_id,
            "reason": reason
            if reason
            in (
                "low_ocr_confidence",
                "unexpected_structure",
                "low_ticker_confidence",
                "missing_source_url",
            )
            else "unexpected_structure",
            "status": "pending",
            "reviewer_notes": notes,
        }
        try:
            resp = self._post_ingest(
                {"type": "review_queue", "payload": payload}
            )
            if resp.status_code in (200, 201):
                return resp.json()
            logger.warning("review_queue HTTP %s: %s", resp.status_code, resp.text[:200])
        except Exception as e:
            logger.warning("review_queue failed: %s", e)
        return None


def get_client() -> HorizonsIngestClient:
    return HorizonsIngestClient()
