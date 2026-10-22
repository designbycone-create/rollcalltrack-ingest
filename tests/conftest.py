"""Pytest stays off the live ingest endpoint."""
import pytest


@pytest.fixture(autouse=True)
def tests_default_to_dry_run(monkeypatch):
    """Drop an ambient INGEST_LIVE so a developer .env cannot make tests post."""
    monkeypatch.delenv("INGEST_LIVE", raising=False)
