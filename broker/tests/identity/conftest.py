"""Owner-account fixtures: an owner, an admin token, a logged-in console client.

These build on the root `env`/`client` fixtures. Later phases that need an
authenticated admin can reuse `admin_headers` (bearer token, CSRF-exempt) or
`session_client` (cookie login; add CSRF_HEADERS on writes).
"""

import time
import types

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

OWNER_USERNAME = "owner"
OWNER_PASSWORD = "correct horse battery staple"
SETUP_TOKEN = "test-setup-token-0123456789abcdef"
CSRF_HEADERS = {"X-Requested-With": "aab-console"}

TEAM = "myteam.cloudflareaccess.com"
AUD = "test-access-aud"


@pytest.fixture(autouse=True)
def _reset_login_limiter():
    """The failed-login limiter is process-global; start every test clean."""
    from broker.identity import ratelimit
    ratelimit.reset()
    yield
    ratelimit.reset()


@pytest.fixture()
def setup_token(env, monkeypatch):
    """SETUP_TOKEN configured in the environment; yields its value."""
    monkeypatch.setenv("SETUP_TOKEN", SETUP_TOKEN)
    from broker.config import get_settings
    get_settings.cache_clear()
    return SETUP_TOKEN


@pytest.fixture()
def owner(env):
    """The owner principal, created directly. Yields id/username/password."""
    from broker.identity import principals
    p = principals.create_owner(OWNER_USERNAME, OWNER_PASSWORD)
    return types.SimpleNamespace(id=p.id, username=p.username, password=OWNER_PASSWORD)


@pytest.fixture()
def admin_token(owner):
    """A freshly minted aab_admin_ token (plaintext) for the owner."""
    from broker.identity import admin_tokens
    return admin_tokens.create(owner.id, "test")["token"]


@pytest.fixture()
def admin_headers(admin_token):
    """Authorization header carrying `admin_token`."""
    return {"Authorization": f"Bearer {admin_token}"}


@pytest.fixture()
def session_client(client, owner):
    """The shared TestClient, logged in as the owner (session cookie in its jar)."""
    r = client.post("/auth/login", json={"username": owner.username,
                                         "password": owner.password})
    assert r.status_code == 200, r.text
    return client


@pytest.fixture()
def cf_identity(env, monkeypatch):
    """Enable Cloudflare Access against a mocked JWKS; yields a token minter.

    Same approach as tests/test_security.py (and WA_GW): a throwaway RSA key
    stands in for the team's signing key.
    """
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
