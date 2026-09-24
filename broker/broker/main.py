"""Broker entrypoint: the FastAPI app wrapped in the origin guard.

Run with exactly ONE uvicorn worker: rate limiting is in-process and SQLite
writes assume a single writer per database.
"""

import asyncio
import contextlib
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import __version__, db, mcp_server, openapi_doc
from .actions import scheduler
from .config import get_settings, validate_exposure
from .errors import PolicyError
from .origin import OriginGuardMiddleware
from .plugins.registry import get_registry, init_registry
from .routers import (actions, admin, admin_keys, admin_ops, admin_plugins, auth, health, me,
                      oauth, permissions, targets)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail closed at boot on unsafe internet-exposure configs (e.g. public mode
    # with the admin plane left on the owner password alone).
    validate_exposure(get_settings())
    db.init()
    # Discover plugin services from env; unreachable ones are retried lazily.
    init_registry()
    task = asyncio.create_task(scheduler.scheduler_loop())
    # Next lane: the Telegram poll loop starts here.
    try:
        # The MCP session manager MUST run inside the app's lifespan, else
        # /mcp requests die with "Task group is not initialized".
        async with mcp_server.run_session_manager():
            yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


# In public mode the interactive API docs (which reveal the full surface) are
# turned off; anyone through the edge could otherwise read them unauthenticated.
_public = get_settings().public_mode()
api = FastAPI(
    title="Agent Authority Broker", version=__version__, lifespan=lifespan,
    docs_url=None if _public else "/docs",
    redoc_url=None if _public else "/redoc",
    openapi_url=None if _public else "/openapi.json",
)

# Every router whose routes form the admin plane; each is guarded router-wide
# by require_admin (tests/identity/test_admin_tokens.py walks this list).
ADMIN_ROUTERS = (admin.router, admin_plugins.router, admin_keys.router, admin_ops.router,
                 oauth.router)

api.include_router(health.router)
# Pre-login owner endpoints (status/setup/login/logout): outside require_admin.
api.include_router(auth.router)
for _r in ADMIN_ROUTERS:
    api.include_router(_r)
# The agent surface (aab_ keys). These routers never import deps.py/identity.
for _r in (targets.router, actions.router, me.router, permissions.router):
    api.include_router(_r)


def _openapi() -> dict:
    # Regenerated per request from the enabled plugins: enabling or disabling
    # a plugin changes the documented surface immediately.
    return openapi_doc.build(api, get_registry().enabled_manifests())


api.openapi = _openapi


@api.exception_handler(PolicyError)
async def policy_error(_: Request, exc: PolicyError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content=exc.body())


@api.exception_handler(RequestValidationError)
async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    # Compact and input-free: pydantic's default body echoes the submitted
    # values back, which agents pay tokens for and which may be sensitive.
    parts = []
    for err in exc.errors()[:5]:
        loc = ".".join(str(x) for x in err.get("loc", ()) if x != "body") or "body"
        parts.append(f"{loc}: {err.get('msg', 'invalid')}")
    return JSONResponse(status_code=422, content={
        "error": "invalid request: " + "; ".join(parts), "code": "invalid_request"})


class BrokerApp:
    """ASGI front door: /mcp (and /mcp/...) goes to the MCP server, everything
    else (and the lifespan, which also runs the MCP session manager) to FastAPI.

    A Starlette Mount("/mcp") would 307-redirect bare "/mcp" to "/mcp/", and
    MCP clients do not reliably follow redirects on POST; hence this splitter.
    """

    def __init__(self, api_app, mcp_app):
        self.api = api_app
        self.mcp_app = mcp_app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and (
                scope["path"] == "/mcp" or scope["path"].startswith("/mcp/")):
            await self.mcp_app(scope, receive, send)
            return
        await self.api(scope, receive, send)


# OriginGuard runs first on every request, API and MCP alike: it enforces the
# Cloudflare origin secret and stamps the trusted client IP into scope state.
app = OriginGuardMiddleware(BrokerApp(api, mcp_server.mcp_app))
