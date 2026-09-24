"""The owner console page: `GET /admin` and every `GET /admin/...` deep link.

The page (templates/console.html) is a static single-file app. It holds no
data: everything it shows comes from the admin API, called with the owner's
session cookie after the page's own setup/login screens. So the page needs
no owner credential to be fetched; like `/auth/*` it does require the
Cloudflare Access identity when Access is enabled.

Every path under /admin serves the same file (the hash router picks the
view), and the path is never used for anything, so there is nothing to
traverse.

Why a per-request nonce rather than `script-src 'unsafe-inline'`: the
console renders text agents control (notes, params, resource labels) on the
page that can approve those agents' requests. The page builds all of it with
textContent, but if an escaping slip ever let markup through, a nonce CSP
still refuses to run an injected <script> or `onerror=` handler, so a slip
cannot become "the agent approves its own request". The page therefore
uses no inline event-handler attributes (a test enforces it).
"""

import secrets
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse

from ..deps import require_cf_access

router = APIRouter(dependencies=[Depends(require_cf_access)], include_in_schema=False)

_TEMPLATE = Path(__file__).resolve().parents[1] / "templates" / "console.html"
NONCE_MARK = "{{NONCE}}"


def csp(nonce: str) -> str:
    """The console's Content-Security-Policy. Nothing loads from outside the
    broker's own origin; `form-action 'none'` means a form whose script
    failed to load can never fall back to a native submit that would put a
    password in a URL."""
    return ("default-src 'self'; "
            f"script-src 'nonce-{nonce}'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "frame-ancestors 'none'; "
            "base-uri 'none'; "
            "form-action 'none'; "
            "object-src 'none'")


def _page() -> HTMLResponse:
    nonce = secrets.token_urlsafe(18)
    html = _TEMPLATE.read_text(encoding="utf-8").replace(NONCE_MARK, nonce)
    return HTMLResponse(html, headers={
        "Content-Security-Policy": csp(nonce),
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
    })


@router.get("/admin", response_class=HTMLResponse)
def console() -> HTMLResponse:
    return _page()


@router.get("/admin/{path:path}", response_class=HTMLResponse)
def console_deep_link(path: str) -> HTMLResponse:
    return _page()
