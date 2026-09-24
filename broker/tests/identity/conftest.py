"""Identity fixtures: the setup token, the login limiter reset, and a mocked
Cloudflare Access identity. The owner/admin fixtures are in the root conftest.
"""

import time
import types

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

# owner / admin_token / admin_headers / session_client now live in the root
# conftest (shared with the engine suites); the constants are re-exported
# here for the identity tests that import them from this module.
from ..conftest import CSRF_HEADERS, OWNER_PASSWORD, OWNER_USERNAME  # noqa: F401

SETUP_TOKEN = "test-setup-token-0123456789abcdef"

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
