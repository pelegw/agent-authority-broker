"""Shared fixtures: a fresh temp database per test and a TestClient.

Later phases add their fixtures here (owner/admin helpers in phase 1, agent
keys in phase 2, the echo plugin in phase 3); keep `env` as the root one.
"""

import pytest
from fastapi.testclient import TestClient

# Exposure settings a developer's shell might carry; tests must start from the
# local, non-public default regardless.
_EXPOSURE_VARS = (
    "ORIGIN_SECRET", "ORIGIN_SECRET_HEADER", "CF_ACCESS_ENABLED",
    "CF_ACCESS_TEAM_DOMAIN", "CF_ACCESS_AUD", "CF_ACCESS_ALLOWED_EMAILS",
    "ALLOW_INSECURE_ADMIN", "TRUST_CF_CONNECTING_IP",
)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Point the app at a fresh per-test database and reset cached settings."""
    for var in _EXPOSURE_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("BROKER_DB", str(tmp_path / "broker.db"))
    # TestClient sends Host: testserver; the MCP transport (phase 3) must accept it.
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "localhost:*,127.0.0.1:*,testserver")
    from broker.config import get_settings
    get_settings.cache_clear()
    from broker import db
    db.init()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def client(env):
    from broker.main import app
    return TestClient(app)
