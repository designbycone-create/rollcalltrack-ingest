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
from typing import Any
from urllib.parse import urlencode

import requests

from . import config
from .names import parse_filer_name, parse_state_district

logger = logging.getLogger("rollcalltrack_ingest.ingest_client")


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


def clean_transaction(txn: dict) -> dict:
    """Map scraper fields onto Horizons/PB select enums."""
    out: dict[str, Any] = {
        "asset_name": txn.get("asset_name") or "",
        "filer_relation": _map_filer_relation(txn.get("filer_relation")),
        "transaction_type": _map_txn_type(txn.get("transaction_type")),
        "transaction_date": txn.get("transaction_date") or "",
        "amount_range_raw": txn.get("amount_range_raw") or "",
        "ticker_confidence": _map_ticker_conf(txn.get("ticker_confidence")),
    }
    if txn.get("notification_date"):
        out["notification_date"] = txn["notification_date"]
    if txn.get("ticker"):
        out["ticker"] = txn["ticker"]
    if txn.get("asset_category"):
        out["asset_category"] = txn["asset_category"]
    if txn.get("amount_range_low") is not None:
        out["amount_range_low"] = txn["amount_range_low"]
    if txn.get("amount_range_high") is not None:
        out["amount_range_high"] = txn["amount_range_high"]
    return out


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

    @property
    def configured(self) -> bool:
        return bool(self.base and self.ingest_key)

    def _get(self, path: str, params: dict | None = None) -> requests.Response:
        url = f"{self.base}{path}"
        if params:
            url = f"{url}?{urlencode(params)}"
        return self.session.get(url, timeout=self.timeout)

    def _post_ingest(self, body: dict, path: str = "/hcgi/api/ingest/disclosures") -> requests.Response:
        return self.session.post(
            f"{self.base}{path}",
            headers={
                "X-Ingest-Key": self.ingest_key,
                "Content-Type": "application/json",
            },
            json=body,
            timeout=self.timeout,
        )

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
            resp.raise_for_status()
        return data

    def submit_votes(self, records: list[dict]) -> dict:
        """POST a batch to /hcgi/api/ingest/votes."""
        if not records:
            return {"ok": True, "written": 0}
        for rec in records:
            if not rec.get("source_url"):
                raise ValueError("source_url is required on every vote")
            if rec.get("jurisdiction") != "state":
                raise ValueError("this worker only posts jurisdiction=state")
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
        if resp.status_code == 404:
            logger.error(
                "Horizons route /hcgi/api/ingest/votes is not deployed (404). "
                "See docs/HORIZONS_LEGISCAN_BILLS.md"
            )
        if resp.status_code not in (200, 201):
            logger.error("Votes ingest HTTP %s: %s", resp.status_code, str(data)[:500])
            resp.raise_for_status()
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

    def lookup_member_by_name(
        self,
        name: str,
        state: str | None = None,
        district: int | None = None,
        office: str | None = None,
        bioguide_id: str | None = None,
        state_district: str | None = None,
    ) -> dict | None:
        """Resolve a filer to a Horizons members row.

        Tries bioguide_id, then multiple name formats (First Last and Last, First),
        then congress roster → bioguide. Logs every attempt.
        """
        if state_district and (state is None or district is None):
            sc, dist = parse_state_district(state_district)
            state = state or sc
            if district is None:
                district = dist

        # 1) bioguide exact
        if bioguide_id:
            member = self._lookup_http(bioguide_id=bioguide_id)
            if member:
                logger.info(
                    "Member lookup bioguide_id=%s → %s (%s)",
                    bioguide_id,
                    member.get("id"),
                    member.get("name"),
                )
                return member

        parsed = parse_filer_name(name, office=office)
        candidates: list[str] = []

        def add(n: str | None) -> None:
            n = (n or "").strip()
            if n and n not in candidates:
                candidates.append(n)

        add(name)
        add(parsed.full)
        if parsed.first and parsed.last:
            add(f"{parsed.first} {parsed.last}")
            add(f"{parsed.last}, {parsed.first}")
            if parsed.middle_tokens:
                mid = " ".join(parsed.middle_tokens)
                add(f"{parsed.last}, {parsed.first} {mid}")
                add(f"{parsed.first} {mid} {parsed.last}")
            # compound last (McClain Delaney)
            tokens = (parsed.full or name).split()
            if len(tokens) >= 3:
                add(f"{tokens[-2]} {tokens[-1]}, {tokens[0]}")
                add(f"{tokens[-1]}, {' '.join(tokens[:-1])}")

        for cand in candidates:
            params: dict[str, Any] = {"name": cand}
            if state:
                params["state"] = state
            if district is not None:
                params["district"] = district
            logger.info(
                "Member lookup attempt name=%r state=%r district=%r",
                cand,
                state,
                district,
            )
            member = self._lookup_http(**params)
            if member:
                logger.info(
                    "Member lookup HIT name=%r → id=%s bioguide=%s stored_name=%r",
                    cand,
                    member.get("id"),
                    member.get("bioguide_id"),
                    member.get("name"),
                )
                return member
            logger.info("Member lookup MISS name=%r", cand)

        # Fallback: congress roster → bioguide
        bid = self._bioguide_from_congress(parsed, state=state, district=district)
        if bid:
            member = self._lookup_http(bioguide_id=bid)
            if member:
                logger.info(
                    "Member lookup via congress bioguide=%s → %s",
                    bid,
                    member.get("name"),
                )
                return member

        logger.warning(
            "Member lookup FAILED for name=%r state=%s district=%s office=%r "
            "candidates_tried=%s",
            name,
            state,
            district,
            office,
            candidates,
        )
        return None

    def _lookup_http(self, **params) -> dict | None:
        # drop Nones
        params = {k: v for k, v in params.items() if v is not None and v != ""}
        if not params:
            return None
        try:
            resp = self._get("/hcgi/api/members/lookup", params)
        except requests.RequestException as e:
            logger.error("lookup request failed: %s", e)
            return None
        if resp.status_code != 200:
            logger.warning("lookup HTTP %s: %s", resp.status_code, resp.text[:200])
            return None
        try:
            data = resp.json()
        except ValueError:
            return None
        if not data.get("found"):
            return None
        if data.get("member"):
            return data["member"]
        members = data.get("members") or []
        return members[0] if members else None

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
    ) -> dict:
        """POST type=filing with nested payload. Returns API JSON body."""
        if not filing.get("source_url"):
            raise ValueError("source_url is required on every filing")

        txns = [clean_transaction(t) for t in (transactions or [])]
        body = {
            "type": "filing",
            "payload": {
                "filing": filing,
                "transactions": txns,
                "annual_assets": annual_assets or [],
            },
        }
        logger.info(
            "Submitting filing source_url=%s member_id=%s bioguide=%s txns=%d",
            filing.get("source_url"),
            filing.get("member_id"),
            filing.get("bioguide_id"),
            len(txns),
        )
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
            logger.warning("ingestion_log submit HTTP %s: %s", resp.status_code, resp.text[:200])
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
