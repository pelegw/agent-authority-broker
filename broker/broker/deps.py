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
