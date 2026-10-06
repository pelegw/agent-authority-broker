"""Admin tokens, session management, and the require_admin guard (incl. CF Access)."""

import hashlib
import json
import re
import time

import pytest
from fastapi.testclient import TestClient

from broker import db
from broker.deps import AdminContext, require_admin
from broker.errors import PolicyError
from broker.identity import admin_tokens, sessions

from .conftest import CSRF_HEADERS, OWNER_PASSWORD


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _new_client():
    from broker.main import app
    return TestClient(app)


# ------------------------------------------------------------ tokens

def test_create_returns_the_plaintext_once(client, admin_headers):
    r = client.post("/v1/admin/tokens", json={"name": "deploy"}, headers=admin_headers)
    assert r.status_code == 200
    created = r.json()
    assert re.fullmatch(r"aab_admin_[0-9a-f]{48}", created["token"])
    assert created["name"] == "deploy" and created["expires_at"] is None
    # Stored only as sha256.
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM admin_tokens WHERE id = ?",
                           (created["id"],)).fetchone()
    assert row["token_hash"] == hashlib.sha256(created["token"].encode()).hexdigest()
    # The new token works.
    assert client.get("/auth/me", headers=_bearer(created["token"])).status_code == 200


def test_list_never_shows_plaintext_or_hash(client, admin_headers):
    created = client.post("/v1/admin/tokens", json={"name": "deploy"},
                          headers=admin_headers).json()
    listed = client.get("/v1/admin/tokens", headers=admin_headers).json()
    assert {t["name"] for t in listed} == {"test", "deploy"}
    for t in listed:
        assert set(t) == {"id", "name", "scope", "created_at", "expires_at",
                          "last_used_at", "revoked", "expired"}
    dump = json.dumps(listed)
    assert created["token"] not in dump
    assert hashlib.sha256(created["token"].encode()).hexdigest() not in dump
    assert "aab_admin_" not in dump


def test_create_is_audited_without_the_token(client, admin_headers, owner):
    created = client.post("/v1/admin/tokens", json={"name": "deploy"},
                          headers=admin_headers).json()
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM audit_log WHERE action = 'admin_token.create'")]
    assert len(rows) == 1
    assert rows[0]["actor"] == "owner" and rows[0]["actor_principal"] == owner.id
    assert rows[0]["actor_via"] == "token"
    assert created["token"] not in json.dumps(rows)


def test_revoked_token_is_401(client, admin_headers, owner):
    extra = admin_tokens.create(owner.id, "extra")
    assert client.get("/auth/me", headers=_bearer(extra["token"])).status_code == 200
    r = client.post(f"/v1/admin/tokens/{extra['id']}/revoke", headers=admin_headers)
    assert r.status_code == 200
    denied = client.get("/auth/me", headers=_bearer(extra["token"]))
    assert denied.status_code == 401
    assert denied.json() == {"error": "admin authentication required", "code": "unauthorized"}
    listed = {t["id"]: t for t in client.get("/v1/admin/tokens", headers=admin_headers).json()}
    assert listed[extra["id"]]["revoked"] is True


def test_revoke_unknown_token_is_404(client, admin_headers):
    assert client.post("/v1/admin/tokens/nope/revoke", headers=admin_headers).status_code == 404


def test_expired_token_is_401(client, admin_headers, owner, monkeypatch):
    extra = admin_tokens.create(owner.id, "short", expires_in_hours=1)
    assert extra["expires_at"] == pytest.approx(time.time() + 3600, abs=5)
    assert client.get("/auth/me", headers=_bearer(extra["token"])).json()["expires_at"] == \
        extra["expires_at"]
    later = int(time.time()) + 3601
    monkeypatch.setattr(admin_tokens, "_now", lambda: later)
    assert client.get("/auth/me", headers=_bearer(extra["token"])).status_code == 401
    listed = {t["id"]: t for t in client.get("/v1/admin/tokens", headers=admin_headers).json()}
    assert listed[extra["id"]]["expired"] is True


def test_expiry_bounds_are_validated(client, admin_headers):
    for bad in (0, -1, 24 * 3650 + 1):
        r = client.post("/v1/admin/tokens", json={"name": "x", "expires_in_hours": bad},
                        headers=admin_headers)
        assert r.status_code == 422, bad


def test_last_used_is_throttled(client, admin_token, monkeypatch):
    now = {"t": 5_000_000}
    monkeypatch.setattr(admin_tokens, "_now", lambda: now["t"])

    def last_used():
        with db.connect() as conn:
            return conn.execute("SELECT last_used_at FROM admin_tokens").fetchone()[0]

    client.get("/auth/me", headers=_bearer(admin_token))
    assert last_used() == now["t"]
    first = now["t"]
    now["t"] += 30
    client.get("/auth/me", headers=_bearer(admin_token))
    assert last_used() == first
    now["t"] += 31
    client.get("/auth/me", headers=_bearer(admin_token))
    assert last_used() == now["t"]


def test_non_admin_bearer_values_are_401(client, owner):
    for header in ("Bearer aab_notadmin", "Bearer ", "Basic abc", "aab_admin_x",
                   "Bearer aab_admin_" + "0" * 48):
        r = client.get("/auth/me", headers={"Authorization": header})
        assert r.status_code == 401, header


def test_non_ascii_authorization_header_denies_cleanly(env):
    # Ported from WA_GW: raw non-ASCII header bytes must give 401, never a 500.
    with pytest.raises(PolicyError) as e:
        require_admin(request=None, authorization="Bearer aab_admin_\xff\xfe",
                      cf_access_jwt_assertion=None)
    assert e.value.status == 401


# ------------------------------------------------------------ AdminContext

def test_require_admin_yields_context_via_token(client, admin_headers, owner):
    me = client.get("/auth/me", headers=admin_headers).json()
    assert me == {"username": "owner", "principal_id": owner.id, "via": "token",
                  "expires_at": None}


def test_require_admin_yields_context_via_session(session_client, owner):
    me = session_client.get("/auth/me").json()
    assert me["via"] == "session" and me["principal_id"] == owner.id


def test_require_admin_returns_admin_context_directly(owner, admin_token):
    ctx = require_admin(request=None, authorization=f"Bearer {admin_token}",
                        cf_access_jwt_assertion=None)
    assert isinstance(ctx, AdminContext)
    assert (ctx.principal_id, ctx.username, ctx.via) == (owner.id, "owner", "token")
    with db.connect() as conn:
        assert ctx.credential_id == conn.execute("SELECT id FROM admin_tokens").fetchone()[0]


def test_disabled_principal_is_401_on_every_credential(session_client, admin_headers):
    with db.connect() as conn:
        conn.execute("UPDATE principals SET disabled = 1")
    assert session_client.get("/auth/me").status_code == 401
    assert _new_client().get("/auth/me", headers=admin_headers).status_code == 401


def _admin_operations():
    """(method, path) for every admin-plane operation the app actually serves.

    Read from the OpenAPI path table (public API) rather than api.routes, which
    in current FastAPI holds included routers lazily.
    """
    from broker.main import api
    ops = []
    for path, methods in api.openapi()["paths"].items():
        if path.startswith(("/v1/admin", "/oauth")) or path == "/auth/me":
            ops += [(m.upper(), path) for m in methods]
    return ops


def test_every_admin_route_is_guarded(env):
    """Structural: every admin path is served by one of the guarded admin
    routers (main.ADMIN_ROUTERS), and every route on them carries
    require_admin."""
    from broker.main import ADMIN_ROUTERS
    served = {(m, p) for m, p in _admin_operations()}
    guarded = set()
    for router in ADMIN_ROUTERS:
        for route in router.routes:
            assert require_admin in [d.call for d in route.dependant.dependencies], route.path
            # OpenAPI prints "{x:path}" converters as "{x}".
            guarded |= {(m, route.path.replace(":path}", "}")) for m in route.methods}
    assert served, "no admin routes found: the check would be vacuous"
    assert served <= guarded, served - guarded
    assert ("POST", "/v1/admin/tokens") in served and ("GET", "/auth/me") in served


def test_every_admin_route_is_401_without_credentials(client, owner):
    ops = _admin_operations()
    assert len(ops) >= 7
    for method, path in ops:
        path = re.sub(r"\{[^}]+\}", "x", path)
        r = client.request(method, path)
        assert r.status_code == 401, (method, path)
        assert r.json() == {"error": "admin authentication required", "code": "unauthorized"}


# ------------------------------------------------------------ sessions

def test_sessions_list_shows_hashes_never_cookies(session_client):
    cookie = session_client.cookies.get(sessions.COOKIE_NAME)
    listed = session_client.get("/v1/admin/sessions").json()
    assert len(listed) == 1
    assert listed[0]["id"] == hashlib.sha256(cookie.encode()).hexdigest()
    assert listed[0]["current"] is True
    assert cookie not in json.dumps(listed)


def test_revoke_another_session(session_client, admin_headers):
    other = _new_client()
    assert other.post("/auth/login", json={"username": "owner",
                                           "password": OWNER_PASSWORD}).status_code == 200
    listed = session_client.get("/v1/admin/sessions").json()
    target = next(s for s in listed if not s["current"])
    r = session_client.post(f"/v1/admin/sessions/{target['id']}/revoke", headers=CSRF_HEADERS)
    assert r.status_code == 200
    assert other.get("/auth/me").status_code == 401
    assert session_client.get("/auth/me").status_code == 200
    # Token callers see every session, none marked current.
    via_token = _new_client().get("/v1/admin/sessions", headers=admin_headers).json()
    assert [s["current"] for s in via_token] == [False]


def test_revoke_unknown_session_is_404(client, admin_headers):
    r = client.post("/v1/admin/sessions/nope/revoke", headers=admin_headers)
    assert r.status_code == 404


# ------------------------------------------------------------ Cloudflare Access

def test_admin_requires_valid_access_jwt(cf_identity, client, admin_headers):
    good = {**admin_headers, "cf-access-jwt-assertion": cf_identity()}
    assert client.get("/v1/admin/tokens", headers=good).status_code == 200


def test_admin_token_alone_is_rejected_when_access_enabled(cf_identity, client, admin_headers):
    # Correct admin token but no Access identity -> 403 (bypass attempt).
    r = client.get("/v1/admin/tokens", headers=admin_headers)
    assert r.status_code == 403
    assert r.json() == {"error": "Cloudflare Access identity required", "code": "forbidden"}


def test_access_rejects_bad_tokens_on_admin_routes(cf_identity, client, admin_headers):
    cases = {
        "expired": cf_identity(exp=int(time.time()) - 10),
        "wrong_aud": cf_identity(aud="someone-elses-app"),
        "wrong_issuer": cf_identity(iss="https://evil.cloudflareaccess.com"),
        "email_not_allowed": cf_identity(email="stranger@example.com"),
        "garbage": "not.a.jwt",
    }
    for label, token in cases.items():
        r = client.get("/v1/admin/tokens",
                       headers={**admin_headers, "cf-access-jwt-assertion": token})
        assert r.status_code == 403, label
        # The verifier's reason is not echoed back to the caller.
        assert r.json()["error"] == "Cloudflare Access identity required", label


def test_access_still_needs_an_owner_credential(cf_identity, client, owner):
    # Valid identity but no owner credential -> 401.
    r = client.get("/v1/admin/tokens", headers={"cf-access-jwt-assertion": cf_identity()})
    assert r.status_code == 401


def test_session_login_also_needs_access_when_enabled(cf_identity, client, owner):
    body = {"username": "owner", "password": OWNER_PASSWORD}
    assert client.post("/auth/login", json=body).status_code == 403
    jwt_header = {"cf-access-jwt-assertion": cf_identity()}
    assert client.post("/auth/login", json=body, headers=jwt_header).status_code == 200
    # The session cookie alone is not enough either: Access is required every time.
    assert client.get("/auth/me").status_code == 403
    assert client.get("/auth/me", headers=jwt_header).status_code == 200


# ------------------------------------------------------------ monitor tokens

def test_monitor_token_has_its_own_prefix_and_scope(client, admin_headers):
    r = client.post("/v1/admin/tokens", json={"name": "robot", "scope": "monitor"},
                    headers=admin_headers)
    assert r.status_code == 200, r.text
    created = r.json()
    assert re.fullmatch(r"aab_monitor_[0-9a-f]{48}", created["token"])
    assert created["scope"] == "monitor"
    listed = {t["name"]: t for t in client.get("/v1/admin/tokens", headers=admin_headers).json()}
    assert listed["robot"]["scope"] == "monitor" and listed["test"]["scope"] == "admin"
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM audit_log WHERE action = 'admin_token.create'")]
    dump = json.dumps(rows)
    assert "monitor" in dump and created["token"] not in dump


def test_monitor_token_never_opens_the_admin_plane(client, admin_headers):
    token = client.post("/v1/admin/tokens", json={"name": "robot", "scope": "monitor"},
                        headers=admin_headers).json()["token"]
    for path in ("/auth/me", "/v1/admin/tokens", "/v1/admin/health", "/v1/admin/plugins"):
        r = client.get(path, headers=_bearer(token))
        assert r.status_code == 401, path
        assert r.json() == {"error": "admin authentication required", "code": "unauthorized"}


def test_token_scope_is_validated(client, admin_headers):
    r = client.post("/v1/admin/tokens", json={"name": "x", "scope": "root"},
                    headers=admin_headers)
    assert r.status_code in (400, 422)


def test_scope_column_is_added_to_an_older_database(client, admin_headers):
    # A broker.db from before the column: every token in it is an admin token.
    with db.connect() as conn:
        conn.execute("ALTER TABLE admin_tokens DROP COLUMN scope")
    db.init()
    with db.connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(admin_tokens)")}
        scopes = {r["scope"] for r in conn.execute("SELECT scope FROM admin_tokens")}
    assert "scope" in cols and scopes == {"admin"}
    assert client.get("/auth/me", headers=admin_headers).status_code == 200
