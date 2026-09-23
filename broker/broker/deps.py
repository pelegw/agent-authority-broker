"""FastAPI dependencies shared by routers.

Phase 0 only needs `client_ip`. Later phases add the auth guards here:
`require_admin` (owner session cookie or aab_admin_ token, plus Cloudflare
Access when enabled, returning an AdminContext) arrives in phase 1, and
`current_auth` (aab_ agent-key bearer auth) in phase 2.
"""

from fastapi import Request


def client_ip(request: Request) -> str:
    """Real client IP, as stamped by OriginGuardMiddleware (CF-Connecting-IP
    when the request is trusted, else the socket peer)."""
    ip = request.scope.get("state", {}).get("client_ip")
    if ip:
        return ip
    return request.client.host if request.client else ""


# ---- phase 2: agent-key bearer auth ---------------------------------------
# Kept at the end of the file so the phase 1 (require_admin) and phase 2
# additions merge without touching each other.

from fastapi import Header, HTTPException  # noqa: E402

from . import auth as _auth  # noqa: E402


def current_auth(request: Request,
                 authorization: str | None = Header(None)) -> "_auth.AuthContext":
    """The calling agent key, or 401. Admin tokens are not agent keys and are
    refused here even when valid."""
    ctx = _auth.authenticate_bearer(authorization, client_ip(request))
    if ctx is None:
        raise HTTPException(401, "missing or invalid API key (Authorization: Bearer aab_...)")
    return ctx
