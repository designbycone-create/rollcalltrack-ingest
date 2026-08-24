"""Offline tests for the 50-state + federal public-records catalog."""
from urllib.parse import urlparse

import pytest

from ingest.legiscan import STATE_CODES
from ingest.public_records_states import (
    CATALOG_PATH,
    is_official_host,
    load_catalog,
    run,
    validate_row,
)


def test_catalog_has_federal_and_fifty_unique_states():
    rows = load_catalog()
    assert CATALOG_PATH.is_file()
    assert len(rows) == 51
    federal = [r for r in rows if r.get("jurisdiction") == "federal"]
    states = [r for r in rows if r.get("jurisdiction") == "state"]
    assert len(federal) == 1
    assert federal[0].get("state_code") in (None, "")
    codes = [r["state_code"] for r in states]
    assert set(codes) == set(STATE_CODES)
    assert len(codes) == len(set(codes))


def test_every_row_has_official_source_and_nonempty_portal():
    for rec in load_catalog():
        src = rec.get("source_url") or ""
        portal = rec.get("portal_url") or ""
        assert src.startswith("http"), rec.get("state_code") or rec.get("name")
        assert portal.startswith("http"), rec.get("state_code") or rec.get("name")
        assert is_official_host(src), src
        assert rec.get("name")
        assert rec.get("statute_citation")


def test_is_official_host_accepts_gov_mil_us():
    assert is_official_host("https://www.foia.gov/")
    assert is_official_host("https://www.alabamaag.gov/page")
    assert is_official_host("https://ago.state.ms.us/")
    assert is_official_host("https://www.army.mil/foia")
    assert not is_official_host("https://en.wikipedia.org/wiki/FOIA")
    assert not is_official_host("https://myflsunshine.com/")
    assert not is_official_host("")


def test_validate_row_rejects_missing_source():
    with pytest.raises(ValueError, match="source_url"):
        validate_row(
            {
                "jurisdiction": "federal",
                "name": "Federal FOIA",
                "portal_url": "https://www.foia.gov/request",
                "source_url": "",
            }
        )


def test_validate_row_requires_state_code_on_state_rows():
    with pytest.raises(ValueError, match="state_code"):
        validate_row(
            {
                "jurisdiction": "state",
                "name": "Alabama Open Records Act",
                "portal_url": "https://www.alabamaag.gov/",
                "source_url": "https://www.alabamaag.gov/",
            }
        )


def test_nj_and_pa_have_official_form_urls():
    by_code = {r["state_code"]: r for r in load_catalog() if r.get("state_code")}
    assert (by_code["NJ"].get("official_form_url") or "").startswith("https://")
    assert (by_code["PA"].get("official_form_url") or "").startswith("https://")


class _FakeClient:
    def __init__(self):
        self.configured = True
        self.batches: list[list[dict]] = []

    def submit_public_records_jurisdictions(self, records):
        self.batches.append(list(records))
        return {"ok": True, "written": len(records)}


def test_run_dry_run_does_not_post():
    client = _FakeClient()
    summary = run(dry_run=True, client=client)
    assert summary["mapped"] == 51
    assert summary["posted"] == 0
    assert summary["dry_run"] is True
    assert client.batches == []


def test_run_posts_all_validated_rows():
    client = _FakeClient()
    summary = run(dry_run=False, client=client)
    posted = [row for batch in client.batches for row in batch]
    assert summary["posted"] == 51
    assert len(posted) == 51
    assert all(r.get("source_url") and r.get("portal_url") for r in posted)
    assert "jurisdiction" in posted[0]
