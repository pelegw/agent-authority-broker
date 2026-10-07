"""Broker entrypoint: the FastAPI app wrapped in the origin guard.

Run with exactly ONE uvicorn worker: rate limiting is in-process and SQLite
writes assume a single writer per database.

Logging is configured first, at import, before the app (or anything that
logs while it is built) exists: uvicorn imports this module after setting up
its own handlers, and configure() replaces them (docs/logging.md).
"""

import logging
from contextlib import asynccontextmanager

from . import logging_setup

logging_setup.configure("broker")

import anyio  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.exceptions import RequestValidationError  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

from . import __version__, background, crypto, db, mcp_server, openapi_doc  # noqa: E402
from .actions import scheduler  # noqa: E402
from .config import get_settings, plugin_services, validate_exposure  # noqa: E402
from .errors import PolicyError  # noqa: E402
from .logging_setup import kv  # noqa: E402
from .notify import telegram_inbound  # noqa: E402
from .origin import OriginGuardMiddleware  # noqa: E402
from .plugins.registry import get_registry, init_registry  # noqa: E402
from .request_log import RequestContextMiddleware  # noqa: E402
from .routers import (actions, admin, admin_install, admin_keys, admin_ops,  # noqa: E402
                      admin_plugins, admin_settings, admin_telegram, auth, delegations, health,
                      me, oauth, permissions, skill, targets)
from .routers import console  # noqa: E402
from .services import plugin_install  # noqa: E402

log = logging.getLogger(__name__)
# Request ids the broker's own background jobs use (actions/scheduler.py,
# notify/telegram_inbound.py, the installer sync in services/plugin_install.py).
# An inbound X-Request-Id with one of these prefixes is replaced, so no
# caller can pass itself off as the scheduler.
BACKGROUND_ID_PREFIXES = ("sched-", "tg-", plugin_install.BACKGROUND_PREFIX)


def log_boot(journal_mode: str) -> None:
    """One line of what this broker runs with. Secrets by NAME only, as set
    or unset (their names are values here, never keys: a `name=value` pair
    with a secret's name is what the redaction backstop masks)."""
    s = get_settings()
    secrets = {"setup_token": s.setup_token, "broker_secrets_key": s.broker_secrets_key,
               "decision_signing_key": s.decision_signing_key,
               "origin_secret": s.origin_secret, "installer_token": s.installer_token}
    log.info("broker starting %s", kv(
        version=__version__, public_mode=s.public_mode(), cf_access=s.cf_access_enabled,
        installer=bool(s.installer_url.strip()),
        allow_insecure_admin=s.allow_insecure_admin, db=s.broker_db,
        journal_mode=journal_mode, secrets_set=sorted(n for n, v in secrets.items() if v),
        secrets_unset=sorted(n for n, v in secrets.items() if not v),
        mcp_allowed_hosts=s.mcp_allowed_hosts, plugin_services=sorted(plugin_services())))
    if journal_mode != "wal":
        log.warning("database is not in WAL mode; concurrent reads will block %s",
                    kv(journal_mode=journal_mode))


def log_registry() -> None:
    reg = get_registry()
    log.info("plugin registry ready %s", kv(
        plugins=sorted(reg.entries()), enabled=reg.enabled_plugins(),
        unreachable=reg.pending_services(), refused=sorted(reg.refused),
        installer_services=reg.dynamic_services()))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail closed at boot on unsafe internet-exposure configs (e.g. public mode
    # with the admin plane left on the owner password alone).
    validate_exposure(get_settings())
    log_boot(db.init())
    # Secrets entered in the console (the Telegram bot token) must stay
    # readable: refuse to boot if they exist but BROKER_SECRETS_KEY is missing.
    crypto.check_boot()
    # Discover plugin services from env; unreachable ones are retried lazily.
    init_registry()
    installer = plugin_install.configured_installer()
    if installer:
        # Then the services the installer installed (GET /services): a
        # plugin installed since this container was created is reached
        # without recreating it. A thread, so a slow installer delays only
        # this boot step; one that is down leaves the env services alone.
        plugin_install.reset_sync()
        try:
            await anyio.to_thread.run_sync(plugin_install.reconcile_services)
        except Exception as exc:
            # Never a reason not to boot: the env services stand, and the
            # sync loop tries again. The type only, as everywhere.
            log.error("installer services not applied at boot %s",
                      kv(error=type(exc).__name__))
    log_registry()
    # Keeps broker.db-wal and -shm in place while the broker runs, so the
    # audit exporter can read the file from its read-only mount (db.hold_open).
    keeper = db.hold_open()
    loops = [background.Loop(scheduler.scheduler_loop),
             # Runs the Telegram poll loop while a bot token is stored and
             # stops it when the token is cleared: no restart is ever needed.
             background.Loop(telegram_inbound.supervise)]
    if installer:
        # Keeps the installer's services current: installs, upgrades and
        # removes apply while the broker runs (no restart, ever).
        loops.append(background.Loop(plugin_install.services_loop))
    try:
        # The MCP session manager MUST run inside the app's lifespan, else
        # /mcp requests die with "Task group is not initialized".
        async with mcp_server.run_session_manager():
            yield
    finally:
        # Each stop waits for the loop's in-flight thread (a delivery, a
        # Telegram tap), so no database work outlives the app.
        for loop in loops:
            await loop.stop()
        keeper.close()
        log.info("broker stopped")


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
# admin_install comes before admin_plugins: routes match in order, and
# GET /v1/admin/plugins/installed must not reach GET /v1/admin/plugins/{plugin}.
ADMIN_ROUTERS = (admin.router, admin_install.router, admin_plugins.router, admin_keys.router,
                 admin_ops.router, admin_telegram.router, admin_settings.router)

api.include_router(health.router)
# Pre-login owner endpoints (status/setup/login/logout): outside require_admin.
api.include_router(auth.router)
# The owner console page: a data-free shell; its data comes from the admin API.
api.include_router(console.router)
# The OAuth callback page: data-free too, and reached by a cross-site redirect
# that carries no SameSite=Strict session cookie, so it cannot require one;
# the connect/finish POST it makes is admin-guarded (routers/oauth.py).
api.include_router(oauth.router)
for _r in ADMIN_ROUTERS:
    api.include_router(_r)
# The agent surface (aab_ keys). These routers never import deps.py/identity.
for _r in (targets.router, actions.router, me.router, permissions.router,
           delegations.router, skill.router):
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
# RequestContext runs next: the request id every line and decision row of the
# request carries, and the access line (with the ip OriginGuard stamped).
app = OriginGuardMiddleware(RequestContextMiddleware(
    BrokerApp(api, mcp_server.mcp_app), access_logger="broker.access",
    reserved_prefixes=BACKGROUND_ID_PREFIXES))
