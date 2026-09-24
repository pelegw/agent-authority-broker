"""`GET /oauth/callback/{service}`: where Google/GitHub send the owner back.

The page is served WITHOUT an owner credential, like the console page, and
for the same reason it has to be: the owner arrives here by a cross-site
redirect from google.com or github.com, and the session cookie is
SameSite=Strict, so the browser does not send it on that navigation. An
owner-guarded page would answer 401 and the connect flow could never
finish. In public mode the Cloudflare Access identity is still required
(`require_cf_access`), exactly as for `/admin` and `/auth/*`.

That is safe because the page holds nothing and can do nothing by itself:
  * the server fills in only the service name (validated, JSON-encoded) and
    a CSP nonce; nothing from the URL is ever rendered into the HTML;
  * its script reads `code`, `state` and `installation_id`, immediately
    strips them from the address bar and history (`history.replaceState`),
    and POSTs them to `/v1/admin/plugins/{service}/connect/finish`, which
    keeps `require_admin` and the CSRF header. That fetch is same-origin, so
    the Strict session cookie IS sent. Without a live session the POST is a
    401 and the page asks the owner to log in in another tab and retry; the
    code stays in the page's memory only;
  * the plugin checks `state` (single use, 10 minutes), so a code planted by
    someone else cannot complete a connection.

The authorization code does appear once in the broker's access log line for
this GET (uvicorn logs the query string). It is single-use, expires within
minutes, and is worthless without the plugin's client secret.
"""

import json
import re
import secrets
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse

from ..deps import require_cf_access
from ..errors import PolicyError

router = APIRouter(dependencies=[Depends(require_cf_access)], include_in_schema=False)

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
        # Referer headers and caches, allow only our own inline script, and
        # never let another site frame the page.
        "Content-Security-Policy": f"default-src 'none'; script-src 'nonce-{nonce}'; "
                                   "style-src 'unsafe-inline'; connect-src 'self'; "
                                   "frame-ancestors 'none'; base-uri 'none'; "
                                   "form-action 'none'",
        "Referrer-Policy": "no-referrer",
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
    })
