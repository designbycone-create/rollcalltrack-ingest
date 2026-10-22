"""Exponential backoff for transient HTTP failures.

Lookup, ingest submits, and Senate filing fetches share this so a 429 or a
dropped connection waits, then tries again, instead of failing the job on
the first blip.
"""
from __future__ import annotations

import random
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests

# Exception class names that mean the peer dropped the socket. requests often
# wraps http.client.RemoteDisconnected inside ConnectionError; sometimes it
# surfaces on its own.
_CONNECTION_NAMES = {
    "RemoteDisconnected",
    "ConnectionError",
    "ProtocolError",
    "ConnectionAbortedError",
    "ConnectionResetError",
    "NewConnectionError",
    "MaxRetryError",
    "ChunkedEncodingError",
    "Timeout",
    "ConnectTimeout",
    "ReadTimeout",
}

# A Retry-After of many minutes would blow the workflow timeout. Honor the
# header up to this ceiling.
_RETRY_AFTER_CAP_SECONDS = 120.0


def is_connection_error(exc: BaseException) -> bool:
    """True for RemoteDisconnected, ConnectionError, and the same wrapped."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (requests.ConnectionError, requests.Timeout)):
            return True
        if type(current).__name__ in _CONNECTION_NAMES:
            return True
        nxt = current.__cause__ or current.__context__
        current = nxt if nxt is not current else None
    return False


def retry_after_seconds(response) -> float | None:
    """Seconds from a Retry-After header (delta-seconds or HTTP-date)."""
    if response is None:
        return None
    headers = getattr(response, "headers", None) or {}
    getter = getattr(headers, "get", None)
    raw = getter("Retry-After") if getter else None
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def backoff_seconds(
    attempt: int,
    response=None,
    *,
    base: float = 1.0,
    cap: float = 60.0,
) -> float:
    """Delay before retry number ``attempt`` (0 = first retry).

    The wait grows as ``base * 2**attempt``, capped, then jittered to the
    upper half of that value so concurrent callers do not line up. A
    Retry-After header is a floor: the wait is never shorter than the
    server asked for, up to two minutes.
    """
    exp = min(cap, base * (2 ** max(0, attempt)))
    jittered = exp * (0.5 + random.random() * 0.5)
    retry_after = retry_after_seconds(response)
    if retry_after is None:
        return jittered
    honored = min(retry_after, max(cap, _RETRY_AFTER_CAP_SECONDS))
    return max(jittered, honored)
