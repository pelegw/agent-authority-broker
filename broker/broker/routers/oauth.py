"""`GET /oauth/callback/{service}`: where Google/GitHub send the owner back.

Part of the admin plane (admin-guarded; behind Cloudflare Access in public
mode). The page itself holds nothing: its script reads `code`, `state` and
`installation_id` from the URL in the browser and POSTs them, with the CSRF
header, to `/v1/admin/plugins/{service}/connect/finish`, which relays them
once to the plugin. The server never renders a query value into the HTML,
so the page cannot reflect anything an attacker put in the URL.
"""

import json
import re
import secrets
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse

from ..deps import require_admin
from ..errors import PolicyError

router = APIRouter(dependencies=[Depends(require_admin)])

_TEMPLATE = Path(__file__).resolve().parents[1] / "templates" / "oauth_callback.html"
_SERVICE_RE = re.compile(r"^[a-z][a-z0-9]*$")


@router.get("/oauth/callback/{service}", response_class=HTMLResponse)
def oauth_callback(service: str) -> HTMLResponse:
    if not _SERVICE_RE.match(service):
        raise PolicyError(404, "no such service", "not_found")
    nonce = secrets.token_urlsafe(16)
    html = (_TEMPLATE.read_text(encoding="utf-8")
            .replace("{{SERVICE_JSON}}", json.dumps(service))
            .replace("{{NONCE}}", nonce))
    return HTMLResponse(html, headers={
        # The authorization code is in this page's URL: keep it out of
        # Referer headers and caches, and allow only our own inline script.
        "Content-Security-Policy": f"default-src 'none'; script-src 'nonce-{nonce}'; "
                                   "style-src 'unsafe-inline'; connect-src 'self'; "
                                   "base-uri 'none'; form-action 'none'",
        "Referrer-Policy": "no-referrer",
        "Cache-Control": "no-store",
    })
