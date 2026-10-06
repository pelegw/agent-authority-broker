"""Internet-exposure security: origin lockdown, Cloudflare Access, fail-closed boot.

Ported from WA_GW. The admin-guard tests (token + Access together) return in
phase 1 with `require_admin`; here Access verification is exercised directly.
"""

import asyncio
import importlib
import time
import types

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

TEAM = "myteam.cloudflareaccess.com"
AUD = "test-access-aud"
EDGE_SECRET = "s3cret-edge-token"


# ---------------------------------------------------------------- origin guard

def _fresh_client():
    from broker.config import get_settings
    from broker.main import app
    get_settings.cache_clear()
    return TestClient(app)


def _ip_echo_client():
    """A tiny app behind the real guard that reports what deps.client_ip saw."""
    from broker.config import get_settings
    from broker.deps import client_ip
    from broker.origin import OriginGuardMiddleware
    get_settings.cache_clear()
    inner = FastAPI()

    @inner.get("/ip")
    def ip(request: Request) -> dict:
        return {"ip": client_ip(request)}

    return TestClient(OriginGuardMiddleware(inner))


def test_origin_secret_blocks_requests_without_the_header(env, monkeypatch):
    monkeypatch.setenv("ORIGIN_SECRET", EDGE_SECRET)
    client = _fresh_client()

    # No edge header -> looks like a direct-to-origin hit -> 403 before routing.
    r = client.get("/v1/anything")
    assert r.status_code == 403
    assert r.json() == {"error": "request did not originate from the trusted edge"}
    # Wrong value -> 403.
    assert client.get("/v1/anything", headers={"x-aab-origin": "nope"}).status_code == 403
    # Correct edge secret -> passes the guard and reaches routing (404: no such route).
    assert client.get("/v1/anything", headers={"x-aab-origin": EDGE_SECRET}).status_code == 404


def test_origin_header_name_is_configurable(env, monkeypatch):
    monkeypatch.setenv("ORIGIN_SECRET", EDGE_SECRET)
    monkeypatch.setenv("ORIGIN_SECRET_HEADER", "x-custom-edge")
    client = _fresh_client()
    assert client.get("/v1/anything", headers={"x-aab-origin": EDGE_SECRET}).status_code == 403
    assert client.get("/v1/anything", headers={"x-custom-edge": EDGE_SECRET}).status_code == 404


def test_no_origin_secret_means_guard_is_off(client):
    assert client.get("/v1/anything").status_code == 404


def test_health_is_exempt_from_origin_secret(env, monkeypatch):
    monkeypatch.setenv("ORIGIN_SECRET", EDGE_SECRET)
    client = _fresh_client()
    assert client.get("/v1/health").status_code == 200  # probes work without the secret
    assert client.get("/health").status_code == 200
    assert client.head("/health").status_code == 200   # UptimeRobot probes with HEAD


def test_cf_connecting_ip_used_only_when_trusted(env, monkeypatch):
    monkeypatch.setenv("ORIGIN_SECRET", EDGE_SECRET)
    client = _ip_echo_client()
    edge = {"x-aab-origin": EDGE_SECRET, "cf-connecting-ip": "203.0.113.9"}
    assert client.get("/ip", headers=edge).json() == {"ip": "203.0.113.9"}


def test_local_mode_honors_cf_connecting_ip(env):
    # With no origin secret the guard treats every request as trusted (there is
    # no edge to prove), so the header is honored unless
    # TRUST_CF_CONNECTING_IP is turned off. Same behaviour as WA_GW.
    client = _ip_echo_client()
    assert client.get("/ip", headers={"cf-connecting-ip": "203.0.113.9"}).json() == {
        "ip": "203.0.113.9"}


def test_cf_connecting_ip_can_be_distrusted(env, monkeypatch):
    monkeypatch.setenv("TRUST_CF_CONNECTING_IP", "false")
    client = _ip_echo_client()
    ip = client.get("/ip", headers={"cf-connecting-ip": "203.0.113.9"}).json()["ip"]
    assert ip != "203.0.113.9"   # the socket peer ("testclient"), not the header


def test_docs_are_disabled_in_public_mode(env, monkeypatch):
    import broker.main as main
    from broker.config import get_settings
    monkeypatch.setenv("ORIGIN_SECRET", EDGE_SECRET)
    get_settings.cache_clear()
    try:
        public = importlib.reload(main)
        client = TestClient(public.app)
        edge = {"x-aab-origin": EDGE_SECRET}
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert client.get(path, headers=edge).status_code == 404, path
    finally:
        monkeypatch.delenv("ORIGIN_SECRET")
        get_settings.cache_clear()
        importlib.reload(main)
    assert TestClient(main.app).get("/openapi.json").status_code == 200


# --------------------------------------------------------------- cloudflare access

@pytest.fixture()
def cf_identity(env, monkeypatch):
    """Enable CF Access and mint valid RS256 tokens against a mocked JWKS."""
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = priv.public_key()

    from broker import cf_access
    cf_access.reset_cache()
    monkeypatch.setattr(cf_access, "_jwks", lambda _domain: types.SimpleNamespace(
        get_signing_key_from_jwt=lambda _t: types.SimpleNamespace(key=pub)))

    monkeypatch.setenv("CF_ACCESS_ENABLED", "true")
    monkeypatch.setenv("CF_ACCESS_TEAM_DOMAIN", TEAM)
    monkeypatch.setenv("CF_ACCESS_AUD", AUD)
    monkeypatch.setenv("CF_ACCESS_ALLOWED_EMAILS", "peleg@wasserman.me")
    from broker.config import get_settings
    get_settings.cache_clear()

    def make_token(**overrides):
        claims = {"aud": AUD, "iss": f"https://{TEAM}",
                  "exp": int(time.time()) + 3600, "email": "peleg@wasserman.me"}
        claims.update(overrides)
        claims = {k: v for k, v in claims.items() if v is not None}
        return jwt.encode(claims, priv, algorithm="RS256")

    return make_token


def test_access_accepts_a_valid_identity(cf_identity):
    from broker import cf_access
    claims = cf_access.verify(cf_identity())
    assert claims["email"] == "peleg@wasserman.me"


def test_access_rejects_missing_assertion(cf_identity):
    from broker import cf_access
    with pytest.raises(cf_access.AccessError):
        cf_access.verify(None)


def test_access_rejects_bad_tokens(cf_identity):
    from broker import cf_access
    cases = {
        "expired": cf_identity(exp=int(time.time()) - 10),
        "wrong_aud": cf_identity(aud="someone-elses-app"),
        "wrong_issuer": cf_identity(iss="https://evil.cloudflareaccess.com"),
        "email_not_allowed": cf_identity(email="stranger@example.com"),
        "missing_exp": cf_identity(exp=None),   # exp must be present, not just valid
        "garbage": "not.a.jwt",
    }
    for label, token in cases.items():
        with pytest.raises(cf_access.AccessError):
            cf_access.verify(token)
            pytest.fail(label)


def test_access_enabled_but_unconfigured_rejects(env, monkeypatch):
    from broker import cf_access
    from broker.config import get_settings
    monkeypatch.setenv("CF_ACCESS_ENABLED", "true")
    get_settings.cache_clear()
    with pytest.raises(cf_access.AccessError, match="not configured"):
        cf_access.verify("anything")


# ------------------------------------------------------ fail-closed interlock

def _settings(**over):
    from broker.config import Settings
    base = dict(origin_secret="", cf_access_enabled=False, cf_access_team_domain="",
                cf_access_aud="", allow_insecure_admin=False)
    base.update(over)
    return Settings(**base)


def test_public_mode_without_access_refuses_to_start(env):
    from broker.config import validate_exposure
    # ORIGIN_SECRET set but no Cloudflare Access -> boot must fail closed.
    with pytest.raises(RuntimeError, match="admin plane"):
        validate_exposure(_settings(origin_secret="edge"))


def test_public_mode_with_access_is_allowed(env):
    from broker.config import validate_exposure
    validate_exposure(_settings(origin_secret="edge", cf_access_enabled=True,
                                cf_access_team_domain="t.cloudflareaccess.com",
                                cf_access_aud="aud"))  # no raise


def test_explicit_override_allows_insecure_admin(env):
    from broker.config import validate_exposure
    validate_exposure(_settings(origin_secret="edge", allow_insecure_admin=True))


def test_access_enabled_but_unconfigured_refuses_to_start(env):
    from broker.config import validate_exposure
    with pytest.raises(RuntimeError, match="CF_ACCESS"):
        validate_exposure(_settings(cf_access_enabled=True))


def test_local_default_is_fine(env):
    from broker.config import validate_exposure
    validate_exposure(_settings())  # nothing set -> no raise


def test_app_boot_fails_closed_in_public_mode_without_access(env, monkeypatch):
    # Same interlock, end to end: the lifespan must refuse to start.
    monkeypatch.setenv("ORIGIN_SECRET", EDGE_SECRET)
    from broker.config import get_settings
    from broker.main import app
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="admin plane"):
        with TestClient(app):
            pass


def test_public_mode_is_driven_by_origin_secret(env):
    assert not _settings().public_mode()
    assert _settings(origin_secret="edge").public_mode()


# ------------------------------------------------------ misc hardening

def test_non_ascii_origin_header_is_rejected_not_500(env, monkeypatch):
    # Raw non-ASCII header bytes only exist at the ASGI layer (httpx blocks
    # them), so drive the middleware directly. A byte like 0xFF must yield a
    # clean 403, never a TypeError -> 500 on every request.
    monkeypatch.setenv("ORIGIN_SECRET", EDGE_SECRET)
    from broker.config import get_settings
    from broker.origin import OriginGuardMiddleware
    get_settings.cache_clear()

    async def dummy(scope, receive, send):  # would only run if the guard passed
        raise AssertionError("request should have been blocked")

    guard = OriginGuardMiddleware(dummy)
    scope = {"type": "http", "path": "/v1/anything", "client": ("198.51.100.7", 5),
             "headers": [(b"x-aab-origin", b"\xff\xfe")]}
    sent = []

    async def send(msg):
        sent.append(msg)

    async def receive():
        return {"type": "http.request"}

    asyncio.run(guard(scope, receive, send))
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 403


def test_health_does_not_leak_link_status(client):
    body = client.get("/v1/health").json()
    assert set(body) == {"status", "version"}  # no plugin/sidecar/connection state
