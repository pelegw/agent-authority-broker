"""Password login, session cookies, expiry, logout, rate limit, CSRF, password change."""

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from broker import db
from broker.identity import principals, sessions

from .conftest import CSRF_HEADERS, OWNER_PASSWORD

NEW_PASSWORD = "a much longer new passphrase"


def _login(client, username="owner", password=OWNER_PASSWORD, **kw):
    return client.post("/auth/login", json={"username": username, "password": password}, **kw)


def _new_client():
    from broker.main import app
    return TestClient(app)


def _audit_rows(action):
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM audit_log WHERE action = ? ORDER BY id", (action,))]


# ------------------------------------------------------------ login

def test_login_sets_a_session_and_me_reports_it(client, owner):
    r = _login(client)
    assert r.status_code == 200
    assert r.json()["username"] == "owner"
    me = client.get("/auth/me").json()
    assert me["username"] == "owner" and me["via"] == "session"
    assert me["principal_id"] == owner.id
    assert me["expires_at"] == r.json()["expires_at"]
    assert client.get("/auth/status").json()["login_required"] is False
    ok = _audit_rows("auth.login")
    assert len(ok) == 1 and ok[0]["actor_principal"] == owner.id
    assert ok[0]["actor_via"] == "session"


def test_wrong_password_is_401_and_audited(client, owner):
    r = _login(client, password="wrong password here")
    assert r.status_code == 401
    assert r.json() == {"error": "invalid username or password", "code": "unauthorized"}
    assert "set-cookie" not in r.headers
    rows = _audit_rows("auth.login_failed")
    assert len(rows) == 1
    assert rows[0]["result"] == "denied"
    assert json.loads(rows[0]["detail"])["username"] == "owner"


def test_password_typed_as_username_is_not_audited(client, owner):
    _login(client, username="My Secret Pass!", password="x")
    detail = json.loads(_audit_rows("auth.login_failed")[0]["detail"])
    assert detail["username"] == "(invalid)"


def test_unknown_user_looks_identical_and_still_runs_scrypt(client, owner, monkeypatch):
    calls = []
    real = principals.hash_password
    monkeypatch.setattr(principals, "hash_password",
                        lambda pw, salt: calls.append(1) or real(pw, salt))

    wrong = _login(client, password="wrong password here")
    assert len(calls) == 1
    unknown = _login(client, username="nobody", password="wrong password here")
    assert len(calls) == 2   # exactly one scrypt on the unknown-user path too
    assert (unknown.status_code, unknown.json()) == (wrong.status_code, wrong.json())


def test_disabled_owner_cannot_log_in(client, owner):
    with db.connect() as conn:
        conn.execute("UPDATE principals SET disabled = 1")
    assert _login(client).status_code == 401


# ------------------------------------------------------------ cookie

def test_cookie_flags_in_local_mode(client, owner):
    cookie = _login(client).headers["set-cookie"]
    parts = [p.strip().lower() for p in cookie.split(";")]
    assert parts[0].startswith("aab_session=")
    assert "httponly" in parts
    assert "samesite=strict" in parts
    assert "path=/" in parts
    assert "secure" not in parts   # plain-HTTP localhost must still work


def test_cookie_is_secure_in_public_mode(env, owner, monkeypatch):
    monkeypatch.setenv("ORIGIN_SECRET", "edge-secret")
    from broker.config import get_settings
    get_settings.cache_clear()
    r = _login(_new_client(), headers={"x-aab-origin": "edge-secret"})
    assert r.status_code == 200
    parts = [p.strip().lower() for p in r.headers["set-cookie"].split(";")]
    assert "secure" in parts and "httponly" in parts and "samesite=strict" in parts


def test_database_stores_only_the_hash_of_the_cookie(client, owner):
    _login(client)
    value = client.cookies.get(sessions.COOKIE_NAME)
    assert len(value) >= 43   # 32 random bytes, urlsafe base64
    with db.connect() as conn:
        ids = [r["id"] for r in conn.execute("SELECT id FROM sessions")]
    assert ids == [hashlib.sha256(value.encode()).hexdigest()]
    # A stored id replayed as a cookie is not a session.
    bad = _new_client()
    bad.cookies.set(sessions.COOKIE_NAME, ids[0])
    assert bad.get("/auth/me").status_code == 401


# ------------------------------------------------------------ expiry

@pytest.fixture()
def clock(monkeypatch):
    """Controllable "now" for the session module. List it BEFORE session_client
    so the login itself happens on the fake clock."""
    state = {"now": 1_000_000}
    monkeypatch.setattr(sessions, "_now", lambda: state["now"])
    return state


def test_idle_expiry(clock, session_client):
    from broker.config import get_settings
    idle = get_settings().session_idle_seconds
    clock["now"] += idle - 1
    assert session_client.get("/auth/me").status_code == 200   # touches last_seen
    clock["now"] += idle - 1
    assert session_client.get("/auth/me").status_code == 200   # still inside the new window
    clock["now"] += idle
    r = session_client.get("/auth/me")
    assert r.status_code == 401
    assert r.json() == {"error": "admin authentication required", "code": "unauthorized"}


def test_absolute_expiry_despite_activity(clock, session_client):
    from broker.config import get_settings
    s = get_settings()
    start = clock["now"]
    # Stay active (well inside the idle window) until just before the absolute cap.
    while clock["now"] + s.session_idle_seconds // 2 < start + s.session_absolute_seconds:
        clock["now"] += s.session_idle_seconds // 2
        assert session_client.get("/auth/me").status_code == 200
    clock["now"] = start + s.session_absolute_seconds
    assert session_client.get("/auth/me").status_code == 401


def test_me_expiry_is_the_earlier_of_idle_and_absolute(clock, session_client):
    from broker.config import get_settings
    s = get_settings()
    me = session_client.get("/auth/me").json()
    assert me["expires_at"] == clock["now"] + min(s.session_idle_seconds,
                                                  s.session_absolute_seconds)


def test_last_seen_is_touched_at_most_once_a_minute(clock, session_client):
    def last_seen():
        with db.connect() as conn:
            return conn.execute("SELECT last_seen_at FROM sessions").fetchone()[0]
    first = last_seen()
    clock["now"] += 30
    session_client.get("/auth/me")
    assert last_seen() == first            # within the throttle: no write
    clock["now"] += 31
    session_client.get("/auth/me")
    assert last_seen() == clock["now"]     # past it: recorded


# ------------------------------------------------------------ logout

def test_logout_kills_the_session(session_client):
    value = session_client.cookies.get(sessions.COOKIE_NAME)
    r = session_client.post("/auth/logout", headers=CSRF_HEADERS)
    assert r.status_code == 200
    assert 'aab_session=""' in r.headers["set-cookie"] or "max-age=0" in \
        r.headers["set-cookie"].lower()
    # Replaying the old cookie value no longer works: the row is gone.
    replay = _new_client()
    replay.cookies.set(sessions.COOKIE_NAME, value)
    assert replay.get("/auth/me").status_code == 401
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    assert len(_audit_rows("auth.logout")) == 1


def test_logout_with_a_cookie_needs_the_csrf_header(session_client):
    assert session_client.post("/auth/logout").status_code == 403
    assert session_client.get("/auth/me").status_code == 200   # still logged in


def test_logout_without_a_session_is_harmless(client):
    assert client.post("/auth/logout").status_code == 200


# ------------------------------------------------------------ rate limit

def test_rate_limit_after_five_failures(client, owner):
    for _ in range(5):
        assert _login(client, password="wrong password here").status_code == 401
    blocked = _login(client)   # even the right password, while throttled
    assert blocked.status_code == 429
    assert blocked.json()["code"] == "rate_limited"
    # Throttled attempts are refused before verification and not audited again.
    assert len(_audit_rows("auth.login_failed")) == 5


def test_rate_limit_window_expires(client, owner, monkeypatch):
    from broker.identity import ratelimit
    t = {"now": 100.0}
    monkeypatch.setattr(ratelimit, "_now", lambda: t["now"])
    for _ in range(5):
        _login(client, password="wrong password here")
    assert _login(client).status_code == 429
    t["now"] += ratelimit.WINDOW_SECONDS
    assert _login(client).status_code == 200


def test_rate_limit_is_per_ip(client, owner):
    for _ in range(5):
        _login(client, password="wrong password here",
               headers={"cf-connecting-ip": "203.0.113.1"})
    assert _login(client, headers={"cf-connecting-ip": "203.0.113.1"}).status_code == 429
    assert _login(client, headers={"cf-connecting-ip": "203.0.113.2"}).status_code == 200


# ------------------------------------------------------------ CSRF

def test_cookie_writes_need_the_csrf_header(session_client):
    r = session_client.post("/v1/admin/tokens", json={"name": "x"})
    assert r.status_code == 403
    assert r.json()["code"] == "csrf"
    wrong = session_client.post("/v1/admin/tokens", json={"name": "x"},
                                headers={"X-Requested-With": "XMLHttpRequest"})
    assert wrong.status_code == 403
    ok = session_client.post("/v1/admin/tokens", json={"name": "x"}, headers=CSRF_HEADERS)
    assert ok.status_code == 200
    # Reads are not state-changing and need no header.
    assert session_client.get("/v1/admin/tokens").status_code == 200


def test_token_writes_are_csrf_exempt(client, admin_headers):
    r = client.post("/v1/admin/tokens", json={"name": "x"}, headers=admin_headers)
    assert r.status_code == 200


def test_invalid_bearer_does_not_fall_back_to_the_cookie(session_client):
    # An Authorization header is the credential when present; a bad one is 401
    # even though a valid session cookie rides along.
    r = session_client.get("/auth/me", headers={"Authorization": "Bearer aab_admin_nope"})
    assert r.status_code == 401


# ------------------------------------------------------------ password change

def test_password_change_requires_the_current_password(session_client):
    r = session_client.post("/v1/admin/password", headers=CSRF_HEADERS,
                            json={"current_password": "not it at all",
                                  "new_password": NEW_PASSWORD})
    assert r.status_code == 403
    assert r.json()["code"] == "wrong_password"
    assert _login(_new_client(), password=OWNER_PASSWORD).status_code == 200


def test_password_change_rejects_a_weak_new_password(session_client):
    r = session_client.post("/v1/admin/password", headers=CSRF_HEADERS,
                            json={"current_password": OWNER_PASSWORD, "new_password": "short"})
    assert r.status_code == 400


def test_password_change_invalidates_other_sessions(session_client):
    other = _new_client()
    assert _login(other).status_code == 200
    r = session_client.post("/v1/admin/password", headers=CSRF_HEADERS,
                            json={"current_password": OWNER_PASSWORD,
                                  "new_password": NEW_PASSWORD})
    assert r.status_code == 200
    assert r.json()["sessions_revoked"] == 1
    assert other.get("/auth/me").status_code == 401            # the other session died
    assert session_client.get("/auth/me").status_code == 200   # the changing one lives
    assert _login(_new_client(), password=OWNER_PASSWORD).status_code == 401
    assert _login(_new_client(), password=NEW_PASSWORD).status_code == 200


def test_password_change_by_token_kills_every_session(session_client, admin_headers):
    fresh = _new_client()
    r = fresh.post("/v1/admin/password", headers=admin_headers,
                   json={"current_password": OWNER_PASSWORD, "new_password": NEW_PASSWORD})
    assert r.status_code == 200
    assert session_client.get("/auth/me").status_code == 401


def test_password_guesses_feed_the_rate_limit(session_client):
    for _ in range(5):
        session_client.post("/v1/admin/password", headers=CSRF_HEADERS,
                            json={"current_password": "guess guess guess",
                                  "new_password": NEW_PASSWORD})
    r = session_client.post("/v1/admin/password", headers=CSRF_HEADERS,
                            json={"current_password": OWNER_PASSWORD,
                                  "new_password": NEW_PASSWORD})
    assert r.status_code == 429
