"""Liveness (`/health`: unauthenticated, minimal, version-bearing) and the
owner's health summary (`GET /v1/admin/health`: live checks, 200 or 503)."""

import base64
import sqlite3
import types

import pytest

import broker
from broker.identity import ratelimit
from broker.notify import telegram
from broker.plugins import settings
from broker.plugins.adapter import AdapterError
from broker.plugins.registry import get_registry
from broker.services import system_health


def test_health_returns_status_and_version(client):
    for path in ("/health", "/v1/health"):
        r = client.get(path)
        assert r.status_code == 200
        assert r.json() == {"status": "ok", "version": broker.__version__}


def test_health_answers_head_with_the_status_alone(client):
    # UptimeRobot and other monitors probe with HEAD; FastAPI does not
    # derive HEAD from GET, so it is explicit, and carries no body.
    for path in ("/health", "/v1/health"):
        r = client.head(path)
        assert (r.status_code, r.content) == (200, b""), path


def test_lifespan_boots_and_initializes_db(env, tmp_path):
    # Entering the TestClient context runs the lifespan (validate_exposure +
    # db.init); a local default config must boot cleanly.
    from fastapi.testclient import TestClient
    from broker.main import app
    with TestClient(app) as c:
        assert c.get("/v1/health").status_code == 200
    assert (tmp_path / "broker.db").exists()


# ---- GET /v1/admin/health: the owner's summary for an uptime monitor -----------

SUMMARY = "/v1/admin/health"


def _down():
    raise AdapterError(503, "plugin service unreachable (ConnectError)")


def test_admin_health_needs_owner_credentials(client, echo_local, make_agent):
    assert client.get(SUMMARY).status_code == 401
    assert client.get(SUMMARY, headers=make_agent().headers).status_code == 401


def test_admin_health_is_ok_when_every_enabled_plugin_answers(client, echo_local, admin_headers):
    r = client.get(SUMMARY, headers=admin_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok" and body["failing"] == []
    assert body["version"] == broker.__version__ and isinstance(body["checked_at"], int)
    assert body["checks"]["database"] == {"ok": True}
    assert body["checks"]["plugins"]["echo"] == {"ok": True, "enabled": True,
                                                 "healthy": True, "connected": True}
    assert body["checks"]["telegram"] == {"ok": True, "enabled": False}


def test_admin_health_refreshes_live_and_fails_on_a_dead_plugin(client, echo_local,
                                                                 admin_headers, monkeypatch):
    alive = echo_local.impl.status
    monkeypatch.setattr(echo_local.impl, "status", _down)
    r = client.get(SUMMARY, headers=admin_headers)
    assert r.status_code == 503, r.text
    body = r.json()
    assert body["status"] == "degraded" and body["failing"] == ["plugins.echo"]
    echo = body["checks"]["plugins"]["echo"]
    assert echo["ok"] is False and echo["healthy"] is False
    assert echo["connected"] is True                   # an outage is not a disconnect
    assert "unreachable" in echo["error"]
    # Stored exactly as POST /v1/admin/plugins/echo/health stores it, so the
    # console's card agrees with the monitor.
    assert get_registry().last_health("echo")["healthy"] is False
    # The container comes back: the next poll is green again, nothing to reset.
    monkeypatch.setattr(echo_local.impl, "status", alive)
    r = client.get(SUMMARY, headers=admin_headers)
    assert r.status_code == 200 and r.json()["failing"] == []
    assert get_registry().last_health("echo")["healthy"] is True


def test_admin_health_counts_an_enabled_but_disconnected_plugin_as_degraded(
        client, echo_local, admin_headers, monkeypatch):
    monkeypatch.setattr(echo_local.impl, "status", lambda: {
        "connected": False, "healthy": True, "health": "waiting for QR pairing"})
    r = client.get(SUMMARY, headers=admin_headers)
    assert r.status_code == 503, r.text
    assert r.json()["checks"]["plugins"]["echo"] == {
        "ok": False, "enabled": True, "healthy": True, "connected": False,
        "health": "waiting for QR pairing"}


def test_admin_health_never_fails_on_a_disabled_plugin(client, echo_local, admin_headers,
                                                       monkeypatch):
    settings.set_enabled("echo", False)
    asked = []
    monkeypatch.setattr(echo_local.impl, "status", lambda: asked.append(1) or _down())
    r = client.get(SUMMARY, headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json()["checks"]["plugins"]["echo"] == {"ok": True, "enabled": False}
    assert asked == []                                 # a disabled plugin is not even asked


@pytest.mark.parametrize("token, linked, running, errors, ok, reason", [
    ("set", True, True, 0, True, None),
    ("set", True, True, 2, True, None),                # a blip is not an outage
    ("set", True, True, 3, False, "3 poll errors in a row"),
    ("set", True, False, 0, False, "poll loop not running"),
    ("set", False, True, 0, False, "chat not linked"),
    ("unreadable", True, True, 0, False, "token unreadable"),
    ("unset", True, False, 0, False, "token unset; poll loop not running"),
])
def test_admin_health_judges_an_enabled_telegram_channel(
        client, echo_local, admin_headers, monkeypatch, token, linked, running, errors, ok, reason):
    monkeypatch.setattr(telegram, "enabled", lambda: True)
    monkeypatch.setattr(telegram, "token_state", lambda: token)
    monkeypatch.setattr(telegram, "linked", lambda: linked)
    monkeypatch.setattr(telegram, "poll_state", lambda: {
        "running": running, "last_ok_at": 1700000000, "last_error": None,
        "consecutive_errors": errors})
    r = client.get(SUMMARY, headers=admin_headers)
    tg = r.json()["checks"]["telegram"]
    assert (r.status_code, tg["ok"]) == (200 if ok else 503, ok), r.text
    assert tg.get("reason") == reason
    assert tg["poll_running"] is running and tg["consecutive_errors"] == errors
    assert r.json()["failing"] == ([] if ok else ["telegram"])


def test_admin_health_reports_a_failing_database_instead_of_crashing(
        client, echo_local, admin_headers, monkeypatch):
    def broken():
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(system_health, "db", types.SimpleNamespace(connect=broken))
    r = client.get(SUMMARY, headers=admin_headers)
    assert r.status_code == 503, r.text
    assert r.json()["checks"]["database"] == {"ok": False, "error": "OperationalError"}
    assert "database" in r.json()["failing"]


def test_admin_health_answers_head_with_the_same_verdict(client, echo_local, admin_headers,
                                                         monkeypatch):
    assert client.head(SUMMARY).status_code == 401
    r = client.head(SUMMARY, headers=admin_headers)
    assert (r.status_code, r.content) == (200, b"")
    monkeypatch.setattr(echo_local.impl, "status", _down)
    r = client.head(SUMMARY, headers=admin_headers)
    assert (r.status_code, r.content) == (503, b"")       # the checks ran: stored too
    assert get_registry().last_health("echo")["healthy"] is False


# ---- monitor tokens: the same paths answer the summary -------------------------

@pytest.fixture()
def monitor_token(client, admin_headers):
    r = client.post("/v1/admin/tokens", json={"name": "robot", "scope": "monitor"},
                    headers=admin_headers)
    assert r.status_code == 200, r.text
    return r.json()["token"]


@pytest.fixture(autouse=True)
def _clean_limiter():
    ratelimit.reset()
    yield
    ratelimit.reset()


def _basic(user, password):
    raw = f"{user}:{password}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode()}


def test_monitor_token_turns_the_probe_into_the_summary(client, echo_local, monitor_token):
    bearer = {"Authorization": f"Bearer {monitor_token}"}
    for path in ("/v1/health", "/health"):
        r = client.get(path, headers=bearer)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "ok" and body["checks"]["plugins"]["echo"]["ok"] is True
        assert body["version"] == broker.__version__
        r = client.head(path, headers=bearer)
        assert (r.status_code, r.content) == (200, b"")
    # Without the token the same path is still the anonymous liveness answer.
    assert client.get("/v1/health").json() == {"status": "ok", "version": broker.__version__}


def test_monitor_token_reports_degraded_as_503(client, echo_local, monitor_token, monkeypatch):
    monkeypatch.setattr(echo_local.impl, "status", _down)
    bearer = {"Authorization": f"Bearer {monitor_token}"}
    r = client.get("/v1/health", headers=bearer)
    assert r.status_code == 503 and r.json()["failing"] == ["plugins.echo"]
    r = client.head("/v1/health", headers=bearer)
    assert (r.status_code, r.content) == (503, b"")
    assert client.get("/v1/health").status_code == 200          # liveness never degrades


def test_monitor_token_as_basic_auth(client, echo_local, monitor_token):
    # UptimeRobot's free plan can send Basic auth but no custom header: the
    # token goes in the password (any user), or in the user for a monitor with
    # a single credential field.
    assert client.get("/v1/health", headers=_basic("uptimerobot", monitor_token)).status_code == 200
    assert client.get("/v1/health", headers=_basic(monitor_token, "")).status_code == 200
    assert client.get("/v1/health", headers=_basic("uptimerobot", "wrong")).status_code == 401
    assert client.get("/v1/health", headers={"Authorization": "Basic not-base64!"}).status_code == 401


def test_other_credentials_are_refused_on_the_probe(client, echo_local, admin_token, make_agent,
                                                    monitor_token):
    # An admin token works nowhere here: the admin plane stays behind Access.
    for headers in ({"Authorization": f"Bearer {admin_token}"}, make_agent().headers,
                    _basic("owner", admin_token),
                    {"Authorization": "Bearer aab_monitor_" + "0" * 48},
                    {"Authorization": "Digest abc"}, {"Authorization": ""}):
        ratelimit.reset()
        r = client.get("/v1/health", headers=headers)
        assert r.status_code == 401, headers
        assert r.json() == {"error": "monitor token required", "code": "unauthorized"}
    ratelimit.reset()
    r = client.head("/v1/health", headers={"Authorization": f"Bearer {admin_token}"})
    assert (r.status_code, r.content) == (401, b"")


def test_bad_monitor_credentials_are_throttled_per_ip(client, echo_local, monitor_token):
    bad = {"Authorization": "Bearer aab_monitor_" + "f" * 48}
    for _ in range(ratelimit.MAX_FAILURES):
        assert client.get("/v1/health", headers=bad).status_code == 401
    assert client.get("/v1/health", headers=bad).status_code == 429
    # The good token is throttled too (same IP); the anonymous probe never is.
    good = {"Authorization": f"Bearer {monitor_token}"}
    assert client.get("/v1/health", headers=good).status_code == 429
    assert client.get("/v1/health").status_code == 200


def test_revoked_monitor_token_is_401(client, echo_local, admin_headers, monitor_token):
    listed = client.get("/v1/admin/tokens", headers=admin_headers).json()
    robot = next(t for t in listed if t["name"] == "robot")
    r = client.post(f"/v1/admin/tokens/{robot['id']}/revoke", headers=admin_headers)
    assert r.status_code == 200
    r = client.get("/v1/health", headers={"Authorization": f"Bearer {monitor_token}"})
    assert r.status_code == 401
