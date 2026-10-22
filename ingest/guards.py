"""Stop fixture rows and test runs from writing to a live endpoint.

A filing or transaction is skipped when its source URL is on example.com or
example.org, the member/filer name is blank, member_id is a placeholder
(starts with Z9 or test), or filing_date is more than a year from the run
date. Posts to any other host require INGEST_LIVE=1 or --live, and tests
cannot opt into that.
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime
from urllib.parse import urlparse

logger = logging.getLogger("rollcalltrack_ingest.guards")

_FIXTURE_HOSTS = ("example.com", "example.org", "example.test")
_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y")


def hostname(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "//" + raw
    return (urlparse(raw).hostname or "").lower().rstrip(".")


def _host_is(host: str, base: str) -> bool:
    return host == base or host.endswith("." + base)


def is_example_source(url: str) -> bool:
    host = hostname(url)
    return _host_is(host, "example.com") or _host_is(host, "example.org")


def is_fixture_host(url: str) -> bool:
    host = hostname(url)
    if host in {"localhost", "127.0.0.1", "::1"}:
        return True
    return any(_host_is(host, base) for base in _FIXTURE_HOSTS)


def member_id_is_placeholder(member_id: str | None) -> bool:
    token = str(member_id or "").strip().lower()
    if not token:
        return False
    return token.startswith("z9") or token.startswith("test")


def _parse_filing_date(raw: str | None) -> date | None:
    text = str(raw or "").strip()
    if not text:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def filing_date_too_far(raw: str | None, today: date | None = None) -> bool:
    parsed = _parse_filing_date(raw)
    if parsed is None:
        return False
    today = today or date.today()
    return abs((parsed - today).days) > 365


def _name_is_empty(record: dict, *, require_name: bool) -> bool:
    keys = ("member_name", "filer_name", "name")
    present = [key for key in keys if key in record]
    if not present:
        return require_name
    return not any(str(record.get(key) or "").strip() for key in present)


def rejection_reason(
    record: dict,
    *,
    require_name: bool,
    today: date | None = None,
    check_date: bool = True,
) -> str | None:
    """Why this filing or transaction must not be posted. None when it is ok."""
    reasons: list[str] = []
    source_url = str(record.get("source_url") or "")
    if source_url and is_example_source(source_url):
        reasons.append(f"source_url host {hostname(source_url)}")
    if _name_is_empty(record, require_name=require_name):
        reasons.append("empty member or filer name")
    member_id = str(record.get("member_id") or "").strip()
    if member_id_is_placeholder(member_id):
        reasons.append(f"placeholder member_id {member_id}")
    if check_date and filing_date_too_far(record.get("filing_date"), today=today):
        reasons.append("filing_date more than 1 year from the run date")
    if not reasons:
        return None
    return "; ".join(reasons)


def skip_reason(
    record: dict,
    *,
    kind: str,
    require_name: bool,
    today: date | None = None,
    check_date: bool = True,
) -> str | None:
    reason = rejection_reason(
        record,
        require_name=require_name,
        today=today,
        check_date=check_date,
    )
    if not reason:
        return None
    logger.warning(
        "Skipping %s source_url=%s member_id=%s: %s",
        kind,
        record.get("source_url") or "",
        record.get("member_id") or "",
        reason,
    )
    return reason


def live_posts_enabled() -> bool:
    return os.environ.get("INGEST_LIVE", "").strip().lower() in {"1", "true", "yes", "on"}


def assert_remote_post_allowed(base_url: str) -> None:
    """Block a write to anything that is not a fixture host.

    Tests always refuse. Other processes refuse unless INGEST_LIVE=1 or --live
    set that variable.
    """
    if is_fixture_host(base_url):
        return
    if os.environ.get("PYTEST_CURRENT_TEST"):
        raise RuntimeError(
            "Refusing to post to production from tests. "
            "Tests default to dry-run and cannot reach a live endpoint."
        )
    if not live_posts_enabled():
        raise RuntimeError(
            "Refusing to post without an explicit live opt-in. "
            "Pass --live or set INGEST_LIVE=1."
        )
