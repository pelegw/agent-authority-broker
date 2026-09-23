"""Liveness endpoints: unauthenticated, minimal, and version-bearing."""

import broker


def test_health_returns_status_and_version(client):
    for path in ("/health", "/v1/health"):
        r = client.get(path)
        assert r.status_code == 200
        assert r.json() == {"status": "ok", "version": broker.__version__}


def test_lifespan_boots_and_initializes_db(env, tmp_path):
    # Entering the TestClient context runs the lifespan (validate_exposure +
    # db.init); a local default config must boot cleanly.
    from fastapi.testclient import TestClient
    from broker.main import app
    with TestClient(app) as c:
        assert c.get("/v1/health").status_code == 200
    assert (tmp_path / "broker.db").exists()
