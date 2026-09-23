"""Broker entrypoint: the FastAPI app wrapped in the origin guard.

Run with exactly ONE uvicorn worker: rate limiting is in-process and SQLite
writes assume a single writer per database.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import __version__, db
from .config import get_settings, validate_exposure
from .errors import PolicyError
from .origin import OriginGuardMiddleware
from .routers import admin, auth, health


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail closed at boot on unsafe internet-exposure configs (e.g. public mode
    # with the admin plane left on the owner password alone).
    validate_exposure(get_settings())
    db.init()
    # Phase 3 starts the scheduler and Telegram poll loop here, and runs the
    # MCP session manager around the yield.
    yield


# In public mode the interactive API docs (which reveal the full surface) are
# turned off; anyone through the edge could otherwise read them unauthenticated.
_public = get_settings().public_mode()
api = FastAPI(
    title="Agent Authority Broker", version=__version__, lifespan=lifespan,
    docs_url=None if _public else "/docs",
    redoc_url=None if _public else "/redoc",
    openapi_url=None if _public else "/openapi.json",
)

api.include_router(health.router)
# Pre-login owner endpoints (status/setup/login/logout): outside require_admin.
api.include_router(auth.router)
# Everything that needs an owner credential, guarded router-wide.
api.include_router(admin.router)


@api.exception_handler(PolicyError)
async def policy_error(_: Request, exc: PolicyError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content=exc.body())


# Phase 3: a `BrokerApp` ASGI splitter goes here, routing /mcp (and /mcp/...)
# to the MCP server and everything else to `api`, so bare "/mcp" is not
# 307-redirected (MCP clients don't reliably follow redirects on POST).
# OriginGuard will then wrap BrokerApp instead of `api`.

# OriginGuard runs first on every request: it enforces the Cloudflare origin
# secret and stamps the trusted client IP into scope state.
app = OriginGuardMiddleware(api)
