"""Ingest auth is the API key. Superuser and admin login are gone."""
import importlib
import os
from pathlib import Path

import pytest

from ingest import config, db, house, senate

_ENV_KEYS = (
    "INGEST_API_KEY",
    "INGEST_KEY",
    "X_INGEST_KEY",
    "X-Ingest-Key",
    "HORIZONS_BASE_URL",
    "HORIZONS_BASE",
    "DATABASE_URL",
    "POCKETBASE_URL",
    "POCKETBASE_ADMIN_EMAIL",
    "POCKETBASE_ADMIN_PASSWORD",
    "POCKETBASE_AUTH_TOKEN",
    "CONGRESS_GOV_API_KEY",
    "LEGISCAN_API_KEY",
)


@pytest.fixture
def fresh_config(monkeypatch):
    originals = {key: os.environ.get(key) for key in _ENV_KEYS}

    def apply(**env):
        for key in _ENV_KEYS:
            monkeypatch.delenv(key, raising=False)
        for key, value in env.items():
            if value is not None:
                monkeypatch.setenv(key, value)
        return importlib.reload(config)

    yield apply
    for key, value in originals.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    importlib.reload(config)


def test_ingest_api_key_wins_over_legacy_name(fresh_config):
    cfg = fresh_config(INGEST_API_KEY="canonical-key", INGEST_KEY="legacy-key")
    assert cfg.INGEST_API_KEY == "canonical-key"


def test_ingest_key_fallback_for_one_release(fresh_config):
    cfg = fresh_config(INGEST_KEY="legacy-key", X_INGEST_KEY="other-key")
    assert cfg.INGEST_API_KEY == "legacy-key"


def test_x_ingest_key_still_accepted(fresh_config):
    cfg = fresh_config(**{"X-Ingest-Key": "header-key"})
    assert cfg.INGEST_API_KEY == "header-key"


def test_live_run_requires_ingest_api_key_not_admin(fresh_config):
    cfg = fresh_config(
        DATABASE_URL="https://example.test/hcgi/platform",
        POCKETBASE_ADMIN_EMAIL="admin@example.test",
        POCKETBASE_ADMIN_PASSWORD="super-secret",
    )
    with pytest.raises(RuntimeError) as exc:
        cfg.require_config(dry_run=False, source="all")
    message = str(exc.value)
    assert "INGEST_API_KEY" in message
    assert "POCKETBASE_ADMIN" not in message


def test_live_run_accepts_horizons_key(fresh_config):
    cfg = fresh_config(
        HORIZONS_BASE_URL="https://example.test",
        INGEST_API_KEY="canonical-key",
    )
    cfg.require_config(dry_run=False, source="all")
    assert cfg.use_horizons_api() is True


def test_dry_run_does_not_require_secrets(fresh_config):
    cfg = fresh_config()
    cfg.require_config(dry_run=True, source="all")


def test_no_superuser_or_admin_login():
    root = Path(__file__).resolve().parents[1] / "ingest"
    text = "\n".join(path.read_text(encoding="utf-8") for path in root.glob("*.py"))
    assert "auth-with-password" not in text
    assert "_superusers" not in text
    assert "POCKETBASE_ADMIN_EMAIL" not in text
    assert "POCKETBASE_ADMIN_PASSWORD" not in text
    assert "POCKETBASE_AUTH_TOKEN" not in text
    assert not hasattr(db, "get_conn")
    assert not hasattr(db.PocketBase, "auth")


def test_house_and_senate_refuse_live_run_without_key(fresh_config):
    fresh_config()
    house_result = house.run(dry_run=False)
    senate_result = senate.run(dry_run=False)
    assert house_result["error"] == "INGEST_API_KEY required"
    assert senate_result["error"] == "INGEST_API_KEY required"
    assert house_result["parsed"] == 0
    assert senate_result["parsed"] == 0
