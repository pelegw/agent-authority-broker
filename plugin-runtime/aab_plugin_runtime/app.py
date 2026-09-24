"""`serve()`: the plugin API (docs/plugin-api.md) around one or more adapters.

One plugin *service* (container) may host several plugin ids: plugin-google
serves gmail, gcal and gdrive from one process because they share one OAuth
credential. Each request names its plugin with the `X-Plugin-Id` header
(optional when the service hosts exactly one).

Security properties this module owns:
  * every endpoint, `/manifests` included, requires `X-Plugin-Token`,
    compared in constant time; an empty configured token refuses to boot;
  * secrets are write-only over the network: `/configure` stores them and
    no endpoint returns them;
  * adapter failures map onto the broker's contract (AdapterError status
    passthrough; unreadable secrets = 503, not performed; anything
    unexpected = 502, unknown outcome, with no internals in the body).
"""

import base64
import hmac
import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from .adapter import PluginAdapter, Result
from .errors import AdapterError
from .secret_store import SecretsUnreadable, SecretStore

log = logging.getLogger("aab_plugin_runtime")

TOKEN_HEADER = "x-plugin-token"
PLUGIN_HEADER = "x-plugin-id"
REQUEST_ID_HEADER = "x-request-id"


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConfigureBody(_Body):
    config: dict = Field(default_factory=dict)
    secrets: dict[str, str | None] = Field(default_factory=dict)


class NormalizeBody(_Body):
    kind: str
    value: str


class ResolveBody(_Body):
    kind: str
    query: str = ""
    limit: int = Field(default=20, ge=1, le=200)
    # "ancestors": return the parent chain of resource `query` (nearest
    # first) instead of a name search; used for `subtree` narrowings.
    relation: str | None = None


class LabelBody(_Body):
    kind: str
    ids: list[str] = Field(max_length=500)


class PerformBody(_Body):
    action: str
    params: dict = Field(default_factory=dict)
    scope: dict = Field(default_factory=dict)


class ConnectStartBody(_Body):
    enabled_plugins: list[str] = Field(default_factory=list)


class ConnectFinishBody(_Body):
    code: str | None = None
    state: str | None = None
    installation_id: str | None = None


class SecretSlot:
    """Read-write handle on one slot, for adapters/connections that persist
    credentials they obtain themselves (refresh tokens, installation ids,
    OAuth state nonces). In-process only; never reachable over HTTP."""

    def __init__(self, store: SecretStore, slot: str):
        self._store, self._slot = store, slot

    def get(self, name: str, default: str | None = None) -> str | None:
        return self._store.read_all(self._slot).get(name, default)

    def set(self, name: str, value: str | None) -> None:
        self._store.write(self._slot, {name: value})

    def wipe(self) -> None:
        self._store.wipe(self._slot)

    def __repr__(self) -> str:
        return f"SecretSlot(slot={self._slot!r})"


def serve(adapters: list[PluginAdapter], token: str, secrets_dir: str | Path,
          secrets_key: str | None) -> FastAPI:
    """Build the plugin API app. Raises at boot on any unsafe configuration."""
    if not isinstance(token, str) or not token.strip():
        raise RuntimeError("plugin token is empty; refusing to serve an open plugin API")
    by_id: dict[str, PluginAdapter] = {}
    for a in adapters:
        pid = a.manifest.get("id") if isinstance(a.manifest, dict) else None
        if not pid or pid in by_id:
            raise RuntimeError(f"adapter manifest id missing or duplicated: {pid!r}")
        by_id[pid] = a
    if not by_id:
        raise RuntimeError("serve() needs at least one adapter")
    store = SecretStore(secrets_dir, secrets_key)
    _bind(by_id, store)
    expected = token.encode()

    app = FastAPI(title="aab plugin runtime", docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.state.secret_store = store     # for tests and the hosting process only

    @app.middleware("http")
    async def _auth(request: Request, call_next):
        supplied = request.headers.get(TOKEN_HEADER, "").encode()
        # compare_digest: no early exit on the first differing byte, so the
        # response time does not leak how much of a guessed token was right.
        if not hmac.compare_digest(supplied, expected):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        response = await call_next(request)
        rid = request.headers.get(REQUEST_ID_HEADER)
        if rid:
            response.headers["X-Request-Id"] = rid[:128]
        return response

    @app.exception_handler(AdapterError)
    async def _adapter_error(_: Request, exc: AdapterError):
        return JSONResponse({"error": exc.message}, status_code=exc.status)

    @app.exception_handler(SecretsUnreadable)
    async def _unreadable(_: Request, exc: SecretsUnreadable):
        return JSONResponse({"error": "credentials unreadable; reconnect required"},
                            status_code=503)

    @app.exception_handler(RequestValidationError)
    async def _bad_body(_: Request, exc: RequestValidationError):
        return JSONResponse({"error": "invalid request body"}, status_code=400)

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, exc: Exception):
        # The request may already have reached the target: unknown outcome.
        # Only the exception type is logged; its text could carry a secret.
        log.error("unexpected adapter failure: %s", type(exc).__name__)
        return JSONResponse({"error": "internal plugin error"}, status_code=502)

    def pick(request: Request) -> tuple[str, PluginAdapter]:
        pid = request.headers.get(PLUGIN_HEADER)
        if pid is None:
            if len(by_id) == 1:
                return next(iter(by_id.items()))
            raise AdapterError(400, "X-Plugin-Id header required: this service hosts "
                                    f"{sorted(by_id)}")
        if pid not in by_id:
            raise AdapterError(404, f"no plugin {pid!r} in this service")
        return pid, by_id[pid]

    def connection(request: Request):
        _, adapter = pick(request)
        conn = getattr(adapter, "connection", None)
        if conn is None:
            raise AdapterError(404, "this plugin has no connection flow")
        return conn

    @app.get("/manifests")
    def manifests() -> dict:
        return {"manifests": [a.manifest for a in by_id.values()]}

    @app.get("/status")
    def status(request: Request) -> dict:
        _, adapter = pick(request)
        try:
            out = dict(adapter.status())
            conn = getattr(adapter, "connection", None)
            if conn is not None:
                out["connection"] = conn.status()
        except SecretsUnreadable:
            return {"connected": False, "healthy": False, "health": "reconnect required"}
        return out

    @app.post("/configure")
    def configure(body: ConfigureBody, request: Request) -> dict:
        pid, adapter = pick(request)
        if body.secrets:
            store.write(pid, body.secrets)
        adapter.configure(body.config, store.reader(pid))
        return {"ok": True}      # never echo secret values

    @app.post("/normalize")
    def normalize(body: NormalizeBody, request: Request) -> dict:
        _, adapter = pick(request)
        return {"id": adapter.normalize(body.kind, body.value)}

    @app.post("/resolve")
    def resolve(body: ResolveBody, request: Request) -> dict:
        _, adapter = pick(request)
        if body.relation == "ancestors":
            fn = getattr(adapter, "ancestors", None)
            return {"ancestors": list(fn(body.kind, body.query)) if fn else []}
        if body.relation is not None:
            raise AdapterError(400, f"unknown relation {body.relation!r}")
        return {"items": list(adapter.resolve(body.kind, body.query, body.limit))}

    @app.post("/label")
    def label(body: LabelBody, request: Request) -> dict:
        _, adapter = pick(request)
        return {"labels": dict(adapter.label(body.kind, body.ids))}

    @app.post("/perform")
    def perform(body: PerformBody, request: Request) -> Any:
        _, adapter = pick(request)
        scope = dict(body.scope)
        scope.setdefault("request_id", request.headers.get(REQUEST_ID_HEADER, ""))
        return _encode(adapter.perform(body.action, body.params, scope))

    @app.post("/connect/start")
    def connect_start(body: ConnectStartBody, request: Request) -> dict:
        return connection(request).start(body.enabled_plugins)

    @app.get("/connect/qr.png")
    def connect_qr(request: Request) -> Response:
        png = connection(request).qr_png()
        # A QR is a pairing secret while valid: never cache it anywhere.
        return Response(png, media_type="image/png",
                        headers={"Cache-Control": "no-store"})

    @app.post("/connect/finish")
    def connect_finish(body: ConnectFinishBody, request: Request) -> dict:
        return connection(request).finish(body.code, body.state, body.installation_id)

    @app.post("/disconnect")
    def disconnect(request: Request) -> dict:
        return connection(request).disconnect()

    return app


def _bind(by_id: dict[str, PluginAdapter], store: SecretStore) -> None:
    """Give adapters and connections that ask for it a read-write slot."""
    for pid, adapter in by_id.items():
        if hasattr(adapter, "bind_secrets"):
            adapter.bind_secrets(SecretSlot(store, pid))
        conn = getattr(adapter, "connection", None)
        if conn is not None and hasattr(conn, "bind_secrets"):
            # A shared connection (gmail/gcal/gdrive) names its own slot.
            conn.bind_secrets(SecretSlot(store, getattr(conn, "slot", None) or pid))


def _encode(result: Any) -> dict:
    if not isinstance(result, Result):
        raise AdapterError(502, "adapter returned an unexpected result type")
    if result.binary is not None:
        return {"binary_b64": base64.b64encode(result.binary).decode("ascii"),
                "mime": result.mime or "application/octet-stream"}
    return {"data": result.data}
