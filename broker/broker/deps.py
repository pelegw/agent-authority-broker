"""FastAPI dependencies shared by routers.

`require_admin` guards the whole management plane and returns an
`AdminContext` saying which human is acting and through which surface.
`require_cf_access` is the Cloudflare Access half on its own, for the
pre-login `/auth/*` endpoints. `client_ip` is the trusted caller address.
Phase 2 appends `current_auth` (aab_ agent-key bearer auth) at the end.
"""

from dataclasses import dataclass
from typing import Literal

from fastapi import Header, Request

from . import cf_access
from .config import get_settings
from .errors import PolicyError
from .identity import admin_tokens, sessions

# Custom header the console sends on every state-changing request. Together with
# SameSite=Strict on the session cookie this is the CSRF defence: a cross-site
# form cannot set custom headers, and a cross-site fetch() that tries would need
# a CORS preflight the broker never approves.
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "aab-console"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


@dataclass(frozen=True)
class AdminContext:
    """Who is acting on the admin plane. Every human action records this."""
    principal_id: str
    username: str
    via: Literal["session", "token"]
    credential_id: str             # sessions.id (a hash) or admin_tokens.id
    expires_at: int | None = None  # when this credential stops working, if ever


def _unauthorized() -> PolicyError:
    # One message for every credential failure: the caller learns that it is not
    # authenticated, never which part (missing, expired, revoked, disabled) failed.
    return PolicyError(401, "admin authentication required", "unauthorized")


def require_cf_access(cf_access_jwt_assertion: str | None = Header(None)) -> None:
    """When Cloudflare Access is enabled, require a valid Access identity.

    Ported from WA_GW's require_admin: this is what makes the admin plane
    unreachable to anyone who bypasses Cloudflare, even holding a valid owner
    credential. The verifier's reason is deliberately not echoed back.
    """
    if not get_settings().cf_access_enabled:
        return
    try:
        # Header name maps: Cf-Access-Jwt-Assertion -> cf_access_jwt_assertion.
        cf_access.verify(cf_access_jwt_assertion)
    except cf_access.AccessError as e:
        raise PolicyError(403, "Cloudflare Access identity required", "forbidden") from e


def require_admin(
    request: Request,
    authorization: str | None = Header(None),
    cf_access_jwt_assertion: str | None = Header(None),
) -> AdminContext:
    """Guard the management plane: an owner session cookie OR an aab_admin_
    bearer token, plus a Cloudflare Access identity when Access is enabled.

    Access is checked FIRST so a caller who bypassed Cloudflare never reaches
    the credential lookup (no DB work, no last-used writes, no oracle).
    """
    require_cf_access(cf_access_jwt_assertion)

    # An Authorization header, when sent, is THE credential: no fallback to the
    # cookie. That keeps the CSRF exemption below tied to a real bearer token.
    if authorization is not None:
        scheme, _, token = authorization.partition(" ")
        if scheme != "Bearer" or not token.strip():
            raise _unauthorized()
        auth = admin_tokens.authenticate(token.strip())
        if auth is None:
            raise _unauthorized()
        return AdminContext(auth.principal_id, auth.username, "token", auth.id,
                            auth.expires_at)

    session = sessions.lookup(request.cookies.get(sessions.COOKIE_NAME))
    if session is None:
        raise _unauthorized()
    # Cookies ride along on cross-site requests; bearer tokens never do, which is
    # why only cookie-authenticated writes need the custom header.
    if request.method not in _SAFE_METHODS and \
            request.headers.get(CSRF_HEADER) != CSRF_VALUE:
        raise PolicyError(403, f"missing {CSRF_HEADER}: {CSRF_VALUE} header", "csrf")
    session = sessions.touch(session)
    return AdminContext(session.principal_id, session.username, "session", session.id,
                        session.expires_at())


# client_ip and current_auth live in agent_auth.py so the agent surface can
# use them without importing identity/ (see that module); re-exported here.
from .agent_auth import client_ip, current_auth  # noqa: E402,F401
