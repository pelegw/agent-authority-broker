"""`serve()`: the plugin API (docs/plugin-api.md) around one or more adapters.

One plugin *service* (container) may host several plugin ids: plugin-google
serves gmail, gcal and gdrive from one process because they share one OAuth
credential. Each request names its plugin with the `X-Plugin-Id` header
(optional when the service hosts exactly one).

Security properties this module owns:
  * every endpoint, `/manifests` included, requires `X-Plugin-Token`,
    compared in constant time; an empty configured token refuses to boot;
  * secrets are write-only over the network: `/configure` stores them and
    no endpoint returns them. A secret field a manifest marks `shared: true`
    goes to the connection's shared slot (plugin-google: `google`), so one
    OAuth client secret serves gmail, gcal and gdrive and is stored once;
  * adapter failures map onto the broker's contract (AdapterError status
    passthrough; unreadable secrets = 503, not performed; anything
    unexpected = 502, unknown outcome, with no internals in the body).

Logging (docs/logging.md): `serve()` configures it for the process as
`plugin-<service>`. Every request runs under the broker's X-Request-Id
(request_log.RequestContextMiddleware), so this service's lines carry the id
of the broker request that caused them; each `/perform` logs its action,
status and duration. Never params, results, secret values or tokens.
"""

import base64
import hmac
import inspect
import logging
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from . import logging_setup
from .adapter import PluginAdapter, Result
from .errors import AdapterError
from .logging_setup import current_request_id, kv, set_actor
from .request_log import RequestContextMiddleware, client_ip
from .secret_store import SecretsUnreadable, SecretStore

log = logging.getLogger("aab_plugin_runtime")

TOKEN_HEADER = "x-plugin-token"
PLUGIN_HEADER = "x-plugin-id"


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
    # Computed by the broker (public: https://<SITE_DOMAIN>/oauth/callback/
    # <service>; local: the request's host). Passed only to connections
    # whose start() takes it; the plugin stores it beside the state nonce.
    redirect_uri: str | None = Field(default=None, max_length=2048)


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


class SharedAwareReader:
    """The SecretReader for a plugin with `shared: true` secret fields: those
    names are read from the shared slot, everything else from the plugin's
    own slot. Read-only, never shows values."""

    def __init__(self, store: SecretStore, own: str, shared: str, names: frozenset[str]):
        self._store, self._own, self._shared, self._names = store, own, shared, names

    def get(self, name: str, default: str | None = None) -> str | None:
        slot = self._shared if name in self._names else self._own
        return self._store.read_all(slot).get(name, default)

    def __repr__(self) -> str:
        return f"SharedAwareReader(slot={self._own!r}, shared={self._shared!r})"


def _service_name(adapters: list[PluginAdapter], service: str | None) -> str:
    """The name this process logs as: given by the plugin package (its
    compose service, e.g. `google`), else the ids it hosts."""
    if service:
        return service
    ids = sorted(str(a.manifest.get("id")) for a in adapters
                 if isinstance(getattr(a, "manifest", None), dict))
    return "+".join(ids) or "unknown"


def _unexpected_response(exc: Exception) -> JSONResponse:
    # The request may already have reached the target: unknown outcome.
    # Only the exception type is logged; its text could carry a secret.
    log.error("unexpected adapter failure %s", kv(error=type(exc).__name__))
    return JSONResponse({"error": "internal plugin error"}, status_code=502)


def serve(adapters: list[PluginAdapter], token: str, secrets_dir: str | Path,
          secrets_key: str | None, *, service: str | None = None) -> FastAPI:
    """Build the plugin API app. Raises at boot on any unsafe configuration."""
    service = _service_name(adapters, service)
    # First, so a boot refusal below is logged in the service's own format.
    logging_setup.configure(f"plugin-{service}")
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
    shared = _shared_fields(by_id)
    store = SecretStore(secrets_dir, secrets_key)
    _bind(by_id, store)
    expected = token.encode()
    actor = f"plugin:{service}"

    app = FastAPI(title="aab plugin runtime", docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.state.secret_store = store     # for tests and the hosting process only

    @app.middleware("http")
    async def _auth(request: Request, call_next):
        supplied = request.headers.get(TOKEN_HEADER, "").encode()
        # compare_digest: no early exit on the first differing byte, so the
        # response time does not leak how much of a guessed token was right.
        if not hmac.compare_digest(supplied, expected):
            log.warning("plugin API call refused: bad or missing X-Plugin-Token %s",
                        kv(path=request.url.path, ip=client_ip(request.scope),
                           header_present=bool(supplied)))
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        set_actor(actor)
        try:
            return await call_next(request)
        except Exception as exc:
            # Mapped here, inside the request context, rather than by an
            # Exception handler (which Starlette runs outside every
            # middleware): the 502 then carries X-Request-Id and the access
            # line and the error line carry the request id.
            return _unexpected_response(exc)

    # Added last, so it is the outermost middleware: the 401 above gets an
    # access line and a request id too.
    app.add_middleware(RequestContextMiddleware, access_logger="aab_plugin_runtime.access")

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
        # Belt and braces: _auth maps these first.
        return _unexpected_response(exc)

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
        slot, names = shared.get(pid, (None, frozenset()))
        own = {k: v for k, v in body.secrets.items() if k not in names}
        common = {k: v for k, v in body.secrets.items() if k in names}
        if own:
            store.write(pid, own)
        if common:
            store.write(slot, common)       # once, in the connection's shared slot
        reader = SharedAwareReader(store, pid, slot, names) if names else store.reader(pid)
        adapter.configure(body.config, reader)
        # Field NAMES only, never a value (secret or not).
        log.info("plugin configured %s", kv(plugin=pid, config_fields=sorted(body.config),
                                            secret_fields=sorted(body.secrets)))
        return {"ok": True}      # never echo secret values

    @app.post("/normalize")
    def normalize(body: NormalizeBody, request: Request) -> dict:
        pid, adapter = pick(request)
        try:
            return {"id": adapter.normalize(body.kind, body.value)}
        except AdapterError as exc:
            if exc.status == 400:
                # The kind only: the value is agent input.
                log.info("normalize refused %s", kv(plugin=pid, kind=body.kind,
                                                    status=exc.status))
            raise

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
        pid, adapter = pick(request)
        scope = dict(body.scope)
        scope.setdefault("request_id", current_request_id() or "")
        started, status = time.perf_counter(), 502
        try:
            out = _encode(adapter.perform(body.action, body.params, scope))
            status = 200
            return out
        except AdapterError as exc:
            status = exc.status
            raise
        except SecretsUnreadable:
            status = 503
            raise
        finally:
            # One line per call: what was asked and how it ended, never the
            # params or the result.
            log.log(logging.WARNING if status >= 500 else logging.INFO, "perform %s", kv(
                plugin=pid, action=body.action, status=status,
                duration_ms=round((time.perf_counter() - started) * 1000)))

    @app.post("/connect/start")
    def connect_start(body: ConnectStartBody, request: Request) -> dict:
        pid, _ = pick(request)
        conn = connection(request)
        if body.redirect_uri is not None and _accepts(conn.start, "redirect_uri"):
            out = conn.start(body.enabled_plugins, redirect_uri=body.redirect_uri)
        else:
            out = conn.start(body.enabled_plugins)
        # The kind only: an OAuth or install URL carries the state nonce.
        log.info("connect started %s", kv(plugin=pid, kind=out.get("kind")
                                          if isinstance(out, dict) else None))
        return out

    @app.get("/connect/qr.png")
    def connect_qr(request: Request) -> Response:
        pid, _ = pick(request)
        png = connection(request).qr_png()
        log.info("pairing QR served %s", kv(plugin=pid))
        # A QR is a pairing secret while valid: never cache it anywhere.
        return Response(png, media_type="image/png",
                        headers={"Cache-Control": "no-store"})

    @app.post("/connect/finish")
    def connect_finish(body: ConnectFinishBody, request: Request) -> dict:
        pid, _ = pick(request)
        try:
            out = connection(request).finish(body.code, body.state, body.installation_id)
        except AdapterError as exc:
            log.warning("connect finish failed %s", kv(plugin=pid, status=exc.status))
            raise
        log.info("connect finished %s", kv(plugin=pid))
        return out

    @app.post("/disconnect")
    def disconnect(request: Request) -> dict:
        pid, _ = pick(request)
        out = connection(request).disconnect()
        log.info("disconnected %s", kv(plugin=pid))
        return out

    log.info("plugin service ready %s", kv(service=service, plugins=sorted(by_id),
                                           secrets_key="set" if secrets_key else "unset"))

    return app


def _shared_fields(by_id: dict[str, PluginAdapter]) -> dict[str, tuple[str, frozenset[str]]]:
    """{plugin id: (shared slot, names of its `shared: true` config fields)}.

    Refuses to boot when a manifest declares shared fields but the adapter's
    connection has no slot, or a slot other than the manifest's
    `connection.shared`: a shared secret would otherwise land in a slot no
    connection reads (or in another service's idea of "shared")."""
    out = {}
    for pid, adapter in by_id.items():
        schema = adapter.manifest.get("config_schema") or []
        names = frozenset(f["name"] for f in schema
                          if isinstance(f, dict) and f.get("shared") and f.get("name"))
        if not names:
            continue
        slot = getattr(getattr(adapter, "connection", None), "slot", None)
        declared = (adapter.manifest.get("connection") or {}).get("shared")
        if not slot or slot != declared:
            raise RuntimeError(f"{pid}: shared config fields need a connection whose slot "
                               f"matches connection.shared ({declared!r})")
        out[pid] = (slot, names)
    return out


def _accepts(fn, name: str) -> bool:
    """Does `fn` take keyword `name`? Connections that need no redirect URI
    (a QR pairing, the echo test plugin) keep their one-argument start()."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return name in params or any(p.kind is p.VAR_KEYWORD for p in params.values())


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
