"""Cloudflare Access (Zero Trust) JWT verification for the management plane.

Cloudflare Access authenticates the human at the edge and forwards a signed
JWT in the `Cf-Access-Jwt-Assertion` header. We verify it at the origin so a
caller who bypasses Cloudflare (hits the origin IP directly) cannot reach the
admin surface even with valid owner credentials:

  * signature — against the team's rotating public keys (JWKS), cached briefly
  * aud       — must equal the Access application's AUD tag
  * iss/exp   — issued by the team domain, not expired
  * email     — optionally restricted to an allow-list

Verification is only engaged when cf_access_enabled is true, so local runs and
tests keep working without it.
"""

import time

import httpx
import jwt
from jwt import PyJWKClient

from .config import get_settings


class AccessError(Exception):
    """Access identity is missing or invalid (-> HTTP 401/403)."""


_jwk_client: PyJWKClient | None = None
_jwk_client_domain: str | None = None


def _jwks(team_domain: str) -> PyJWKClient:
    """Cached JWKS client for the team's Access certs endpoint."""
    global _jwk_client, _jwk_client_domain
    if _jwk_client is None or _jwk_client_domain != team_domain:
        url = f"https://{team_domain}/cdn-cgi/access/certs"
        # PyJWKClient caches keys and refreshes on unknown kid (key rotation).
        _jwk_client = PyJWKClient(url, cache_keys=True, lifespan=600)
        _jwk_client_domain = team_domain
    return _jwk_client


def verify(token: str | None) -> dict:
    """Verify an Access JWT and return its claims, or raise AccessError."""
    s = get_settings()
    if not token:
        raise AccessError("missing Cloudflare Access assertion")
    if not s.cf_access_team_domain or not s.cf_access_aud:
        raise AccessError("Cloudflare Access is enabled but not configured")
    try:
        signing_key = _jwks(s.cf_access_team_domain).get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=s.cf_access_aud,
            issuer=f"https://{s.cf_access_team_domain}",
            # Require these claims to be PRESENT, not just valid-if-present, so a
            # token minus exp/aud/iss can't slip through.
            options={"require": ["exp", "aud", "iss"]},
        )
    except AccessError:
        raise
    except (jwt.PyJWTError, httpx.HTTPError) as e:
        raise AccessError(f"invalid Access assertion: {e}") from e
    except Exception as e:  # never fail open on an unexpected verifier error
        raise AccessError(f"Access verification error: {e}") from e

    allowed = [e.strip().lower() for e in s.cf_access_allowed_emails.split(",") if e.strip()]
    if allowed:
        email = str(claims.get("email", "")).lower()
        if email not in allowed:
            raise AccessError(f"identity {email or '(none)'} is not permitted")
    return claims


def reset_cache() -> None:
    """Tests call this after swapping the JWKS source."""
    global _jwk_client, _jwk_client_domain
    _jwk_client = None
    _jwk_client_domain = None


# Kept for symmetry / future skew handling.
def _not_expired(claims: dict) -> bool:
    return int(claims.get("exp", 0)) > int(time.time())
