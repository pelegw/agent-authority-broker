"""FastAPI dependencies shared by routers.

`require_admin` guards the whole management plane and returns an
`AdminContext` saying which human is acting and through which surface.
`require_cf_access` is the Cloudflare Access half on its own, for the
pre-login `/auth/*` endpoints. `client_ip` is the trusted caller address.
Phase 2 appends `current_auth` (aab_ agent-key bearer auth) at the end.
"""

import logging
from dataclasses import dataclass
from typing import Literal

from fastapi import Header, Request

from . import cf_access
from .config import get_settings
from .errors import PolicyError
from .identity import admin_tokens, sessions
from .logging_setup import kv, set_actor

log = logging.getLogger(__name__)

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
    # telegram: a tap by the linked Telegram user (notify/telegram_inbound.py),
    # which is bound to the owner principal when the chat is linked.
    via: Literal["session", "token", "telegram"]
    credential_id: str             # sessions.id (a hash), admin_tokens.id, or the Telegram user id
    expires_at: int | None = None  # when this credential stops working, if ever


def _unauthorized(request: Request | None, reason: str) -> PolicyError:
    # One message for every credential failure: the caller learns that it is not
    # authenticated, never which part (missing, expired, revoked, disabled)
    # failed. The operator's log line says which class it was.
    log.warning("admin authentication failed %s", kv(
        reason=reason, path=request.url.path if request is not None else None,
        ip=client_ip(request) if request is not None else None))
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
        # The error class only: the verifier's text can quote the token.
        log.warning("cloudflare access identity refused %s", kv(error=type(e).__name__))
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
            raise _unauthorized(request, "malformed")
        auth = admin_tokens.authenticate(token.strip())
        if auth is None:
            raise _unauthorized(request, "unknown_or_revoked_token")
        set_actor(f"owner:{auth.username}")
        return AdminContext(auth.principal_id, auth.username, "token", auth.id,
                            auth.expires_at)

    cookie = request.cookies.get(sessions.COOKIE_NAME)
    session = sessions.lookup(cookie)
    if session is None:
        raise _unauthorized(request, "invalid_or_expired_session" if cookie else "no_session")
    # Cookies ride along on cross-site requests; bearer tokens never do, which is
    # why only cookie-authenticated writes need the custom header.
    if request.method not in _SAFE_METHODS and \
            request.headers.get(CSRF_HEADER) != CSRF_VALUE:
        log.warning("admin request refused: missing CSRF header %s",
                    kv(path=request.url.path, ip=client_ip(request)))
        raise PolicyError(403, f"missing {CSRF_HEADER}: {CSRF_VALUE} header", "csrf")
    session = sessions.touch(session)
    set_actor(f"owner:{session.username}")
    return AdminContext(session.principal_id, session.username, "session", session.id,
                        session.expires_at())


# client_ip and current_auth live in agent_auth.py so the agent surface can
# use them without importing identity/ (see that module); re-exported here.
from .agent_auth import client_ip, current_auth  # noqa: E402,F401
