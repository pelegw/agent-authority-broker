"""The MCP surface: a second view over the same registry and dispatch as REST.

Transport: the official SDK's low-level `Server` behind a stateless
`StreamableHTTPSessionManager`. Not FastMCP's decorator registry, because
that fixes the tool list at import time, and here it must be computed per
request: enabled plugins intersected with the caller's effective
capabilities (mcp_tools.py). Stateless HTTP has no session to push
`tools/list_changed` to, and a disabled plugin must expose nothing, so the
list is rebuilt on every `tools/list` and every call is gated again by the
engine.

Authentication happens in `MCPAuthMiddleware` (ported from WA_GW) before
the SDK sees the request: the Bearer `aab_` key becomes an `AuthContext` in
a ContextVar. The per-request server task is spawned from the request's own
task, so it inherits that context; if it ever did not, `_auth()` fails
closed. Blocking work (SQLite, plugin HTTP) always runs on the threadpool
(`_run`), never on the single event loop.

Every refusal is an `isError` result whose text is the same compact
`{"error", "code", "hint"?}` JSON the REST route returns; never a stack
trace or an exception message from inside the broker.

There is NO approve/reject tool, and this module's import graph must not
reach services/admin.py or identity/ (tests/test_no_admin_from_agent_paths.py).
"""

from __future__ import annotations

import contextlib
import functools
import json
import logging
from contextvars import ContextVar
from typing import Any, AsyncIterator

import anyio.to_thread
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings

from . import __version__, mcp_tools
from .agent_auth import authenticate
from .auth import AuthContext
from .config import get_settings
from .errors import PolicyError

log = logging.getLogger(__name__)

CURRENT_AUTH: ContextVar[AuthContext | None] = ContextVar("aab_mcp_auth", default=None)

server: Server = Server("agent-authority-broker", version=__version__)


def _auth() -> AuthContext:
    ctx = CURRENT_AUTH.get()
    if ctx is None:              # unreachable behind the middleware; fail closed anyway
        raise PolicyError(401, "missing or invalid API key", "unauthorized")
    return ctx


async def _run(fn, *args, **kwargs) -> Any:
    """Run a blocking call on the threadpool (anyio copies the contextvars
    into the worker thread)."""
    return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return await _run(mcp_tools.tools_for, _auth())


# validate_input=False: the SDK validates against a tool cache that is
# shared by every caller of this process-wide Server, which is wrong for a
# per-caller list. The engine (and the generic tools' models) validate
# strictly anyway, with the same compact errors as REST.
@server.call_tool(validate_input=False)
async def call_tool(name: str, arguments: dict | None) -> types.CallToolResult:
    try:
        return await _run(mcp_tools.dispatch, _auth(), name, arguments or {})
    except PolicyError as exc:
        return mcp_tools.text_result(exc.body(), error=True)
    except Exception:
        # Log it here (without the arguments, which may carry message text),
        # but never hand the agent a trace or an internal message.
        log.exception("MCP tool %s failed", name)
        return mcp_tools.text_result({"error": "internal error", "code": "internal"},
                                     error=True)


# ---- transport ------------------------------------------------------------------------

def transport_security() -> TransportSecuritySettings:
    """DNS-rebinding protection: only Host headers in MCP_ALLOWED_HOSTS are
    served (a hostile web page resolving its own name to 127.0.0.1 would
    otherwise reach a local broker from the owner's browser)."""
    hosts = [h.strip() for h in get_settings().mcp_allowed_hosts.split(",") if h.strip()]
    return TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                     allowed_hosts=hosts)


class _Manager:
    current: StreamableHTTPSessionManager | None = None


@contextlib.asynccontextmanager
async def run_session_manager() -> AsyncIterator[StreamableHTTPSessionManager]:
    """Run a fresh session manager for the app's lifetime (main.py lifespan).

    A manager's run() works once per instance, so each lifespan gets a new
    one; that also re-reads MCP_ALLOWED_HOSTS at startup."""
    mgr = StreamableHTTPSessionManager(app=server, stateless=True, json_response=True,
                                       security_settings=transport_security())
    async with mgr.run():
        _Manager.current = mgr
        try:
            yield mgr
        finally:
            _Manager.current = None


async def _json(send, status: int, body: dict) -> None:
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": json.dumps(body).encode()})


async def handle_request(scope, receive, send) -> None:
    mgr = _Manager.current
    if mgr is None:
        await _json(send, 503, {"error": "MCP transport is not running", "code": "unavailable"})
        return
    await mgr.handle_request(scope, receive, send)


class MCPAuthMiddleware:
    """ASGI wrapper: Bearer `aab_` key -> AuthContext ContextVar, else 401."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        authorization = None
        for k, v in scope.get("headers", []):
            if k.lower() == b"authorization":
                authorization = v.decode("latin1")
                break
        # OriginGuardMiddleware ran first and stamped the trusted client IP.
        ip = scope.get("state", {}).get("client_ip") or \
            (scope["client"][0] if scope.get("client") else "")
        try:
            # authenticate hits SQLite; keep that blocking call off the loop.
            ctx = await anyio.to_thread.run_sync(authenticate, authorization, ip)
        except PolicyError as exc:
            await _json(send, exc.status, exc.body())
            return
        token = CURRENT_AUTH.set(ctx)
        try:
            await self.app(scope, receive, send)
        finally:
            CURRENT_AUTH.reset(token)


mcp_app = MCPAuthMiddleware(handle_request)
