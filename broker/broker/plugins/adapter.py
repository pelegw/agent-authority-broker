"""The broker's view of a plugin: the `Adapter` protocol and its two transports.

  * `RemoteAdapter` talks to a plugin service over the internal plugin API
    (docs/plugin-api.md). This is the production path: every plugin runs in
    its own container and the broker holds no target credential.
  * `InProcessAdapter` wraps a plugin-side adapter object living in this
    process. Only the `echo` test plugin uses it, so the fast suite needs no
    HTTP; it passes the same JSON-shaped scope a remote plugin would get.

Error contract (the engine depends on it, see CLAUDE.md):
  4xx  passthrough from the plugin (400 bad input, 403, 404 missing/hidden)
  503  NOT performed, safe to retry: plugin unreachable or said so
  502  outcome UNKNOWN: the request may have reached the target (timeout
       after sending, broken response, any unexpected plugin failure);
       never retried automatically
The plugin token is sent as a header and never appears in an error message,
a log line or a repr.

Encoding: a request body is UTF-8 JSON with no NaN/Infinity (`encodable`).
Anything else (a lone surrogate from an agent's JSON, say) is a 400 raised
here, before the network, never an unhandled exception. The engine applies
the same rule to agent input before evaluating it, so in practice nothing
unencodable gets this far.
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from .manifest import Manifest

log = logging.getLogger(__name__)


class AdapterError(Exception):
    """A plugin refused or failed. `status` follows the contract above."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status if isinstance(status, int) and 400 <= status <= 599 else 502
        self.message = message


UNENCODABLE = "params must be valid UTF-8 JSON (no lone surrogates, NaN or Infinity)"


def encode_json(value: Any) -> bytes:
    """The exact bytes a plugin request carries (httpx's own rules: UTF-8, no
    NaN/Infinity). Raises TypeError/ValueError when `value` has no such form."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def encodable(value: Any) -> bool:
    """Can `value` cross the plugin API at all? (UnicodeEncodeError is a
    ValueError; RecursionError covers absurd nesting.)"""
    try:
        encode_json(value)
    except (TypeError, ValueError, RecursionError):
        return False
    return True


@dataclass(frozen=True)
class CallScope:
    """Everything a plugin needs to stay inside the decision, per call.

    visibility   {kind: {"deny": [ids], "allow_only": [ids] | None}}; kind is
                 a resource kind (or a pattern dimension's name). Deny wins.
    constraints  the covering capability's constraints that apply to the action
    credential   requirements for the connection to mint, e.g.
                 {"permissions": {"items": "write"}}; never a credential
    request_id   links the plugin call to the decision record
    """
    request_id: str
    visibility: dict[str, dict[str, Any]] = field(default_factory=dict)
    constraints: dict[str, Any] = field(default_factory=dict)
    credential: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict:
        return {"request_id": self.request_id, "visibility": self.visibility,
                "constraints": self.constraints, "credential": self.credential}


@dataclass(frozen=True)
class Result:
    """What perform returns: JSON `data`, or `binary` bytes with a `mime`."""
    data: Any = None
    binary: bytes | None = None
    mime: str | None = None


class Adapter(Protocol):
    """One plugin id, however it is reached."""
    manifest: Manifest
    service: str

    def configure(self, config: dict, secrets: dict[str, str | None]) -> None: ...
    def status(self) -> dict: ...
    def normalize(self, kind: str, value: str) -> str: ...
    def resolve(self, kind: str, query: str, limit: int) -> list[dict]: ...
    def ancestors(self, kind: str, resource_id: str) -> list[str]: ...
    def label(self, kind: str, ids: list[str]) -> dict[str, str]: ...
    def perform(self, action: str, params: dict, scope: CallScope) -> Result: ...
    def connect_start(self, enabled_plugins: list[str]) -> dict: ...
    def connect_finish(self, code: str | None, state: str | None,
                       installation_id: str | None) -> dict: ...
    def connect_qr(self) -> bytes: ...
    def disconnect(self) -> dict: ...


# ---- in-process ----------------------------------------------------------------

class _MemorySecrets:
    """Secret reader for in-process plugins (tests only). Values live in this
    object's memory, never in broker.db."""

    def __init__(self):
        self._values: dict[str, str] = {}

    def update(self, values: dict[str, str | None]) -> None:
        for k, v in values.items():
            if v in (None, ""):
                self._values.pop(k, None)
            else:
                self._values[k] = v

    def get(self, name: str, default: str | None = None) -> str | None:
        return self._values.get(name, default)

    def __repr__(self) -> str:
        return "_MemorySecrets(<redacted>)"


class InProcessAdapter:
    """Wrap a plugin-side adapter object (the `aab_plugin_runtime` shape)."""

    def __init__(self, impl: Any, manifest: Manifest, service: str = "inprocess"):
        self.impl = impl
        self.manifest = manifest
        self.service = service
        self._secrets = _MemorySecrets()

    def _call(self, fn: Callable, *args):
        try:
            return fn(*args)
        except AdapterError:
            raise
        except Exception as exc:
            status = getattr(exc, "status", None)
            if isinstance(status, int) and 400 <= status <= 599:
                raise AdapterError(status, getattr(exc, "message", str(exc))) from exc
            # Same rule as the runtime: an unexpected failure is an unknown outcome.
            log.error("in-process plugin %s failed: %s", self.manifest.id, type(exc).__name__)
            raise AdapterError(502, "internal plugin error") from exc

    def _connection(self):
        conn = getattr(self.impl, "connection", None)
        if conn is None:
            raise AdapterError(404, "this plugin has no connection flow")
        return conn

    def configure(self, config: dict, secrets: dict[str, str | None]) -> None:
        self._secrets.update(secrets or {})
        self._call(self.impl.configure, dict(config), self._secrets)

    def status(self) -> dict:
        out = dict(self._call(self.impl.status))
        conn = getattr(self.impl, "connection", None)
        if conn is not None:
            out["connection"] = self._call(conn.status)
        return out

    def normalize(self, kind: str, value: str) -> str:
        return self._call(self.impl.normalize, kind, value)

    def resolve(self, kind: str, query: str, limit: int) -> list[dict]:
        return list(self._call(self.impl.resolve, kind, query, limit))

    def ancestors(self, kind: str, resource_id: str) -> list[str]:
        fn = getattr(self.impl, "ancestors", None)
        return list(self._call(fn, kind, resource_id)) if fn else []

    def label(self, kind: str, ids: list[str]) -> dict[str, str]:
        return dict(self._call(self.impl.label, kind, list(ids)))

    def perform(self, action: str, params: dict, scope: CallScope) -> Result:
        # Same rule as the remote transport, so the two cannot diverge.
        if not encodable(params):
            raise AdapterError(400, UNENCODABLE)
        # The JSON round-trip of the scope is deliberate: the plugin sees
        # exactly the shape a remote plugin would, and cannot mutate ours.
        out = self._call(self.impl.perform, action, dict(params), _jsonable(scope.to_json()))
        return Result(data=getattr(out, "data", None), binary=getattr(out, "binary", None),
                      mime=getattr(out, "mime", None))

    def connect_start(self, enabled_plugins: list[str]) -> dict:
        return self._call(self._connection().start, list(enabled_plugins))

    def connect_finish(self, code, state, installation_id) -> dict:
        return self._call(self._connection().finish, code, state, installation_id)

    def connect_qr(self) -> bytes:
        return self._call(self._connection().qr_png)

    def disconnect(self) -> dict:
        return self._call(self._connection().disconnect)


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value))


# ---- remote ----------------------------------------------------------------------

ClientFactory = Callable[[str, dict, float], httpx.Client]


def _default_client(base_url: str, headers: dict, timeout: float) -> httpx.Client:
    return httpx.Client(base_url=base_url, headers=headers, timeout=timeout)


class RemoteAdapter:
    """HTTP client for one plugin id hosted by a plugin service.

    `client_factory(base_url, headers, timeout)` exists for tests, which hand
    in a TestClient bound to the runtime app so no socket is opened.
    """

    def __init__(self, service: str, base_url: str, token: str, manifest: Manifest,
                 timeout: float | Callable[[], float] = 30.0,
                 client_factory: ClientFactory | None = None):
        self.service = service
        self.base_url = base_url
        self.manifest = manifest
        self._token = token
        # A callable is read per call: the registry passes the live console
        # setting (plugin_timeout_seconds), so an edit applies without restart.
        self._timeout = timeout
        self._factory = client_factory or _default_client

    def __repr__(self) -> str:        # the token must never appear here
        return f"RemoteAdapter(service={self.service!r}, plugin={self.manifest.id!r})"

    # ---- transport ----------------------------------------------------------

    def _request(self, method: str, path: str, *, json_body: Any = None,
                 request_id: str | None = None, raw: bool = False):
        return request(self.base_url, self._token, method, path, json_body=json_body,
                       plugin_id=self.manifest.id, request_id=request_id, raw=raw,
                       timeout=self._timeout() if callable(self._timeout) else self._timeout,
                       factory=self._factory)

    # ---- protocol -------------------------------------------------------------

    def configure(self, config: dict, secrets: dict[str, str | None]) -> None:
        self._request("POST", "/configure", json_body={"config": config,
                                                       "secrets": secrets or {}})

    def status(self) -> dict:
        return self._request("GET", "/status")

    def normalize(self, kind: str, value: str) -> str:
        out = self._request("POST", "/normalize", json_body={"kind": kind, "value": value})
        if not isinstance(out.get("id"), str) or not out["id"]:
            raise AdapterError(502, "plugin returned no normalized id")
        return out["id"]

    def resolve(self, kind: str, query: str, limit: int) -> list[dict]:
        out = self._request("POST", "/resolve", json_body={"kind": kind, "query": query,
                                                           "limit": limit})
        return [i for i in out.get("items", []) if isinstance(i, dict)]

    def ancestors(self, kind: str, resource_id: str) -> list[str]:
        out = self._request("POST", "/resolve", json_body={
            "kind": kind, "query": resource_id, "limit": 200, "relation": "ancestors"})
        return [a for a in out.get("ancestors", []) if isinstance(a, str)]

    def label(self, kind: str, ids: list[str]) -> dict[str, str]:
        out = self._request("POST", "/label", json_body={"kind": kind, "ids": list(ids)})
        labels = out.get("labels", {})
        return {k: v for k, v in labels.items() if isinstance(v, str)} if isinstance(
            labels, dict) else {}

    def perform(self, action: str, params: dict, scope: CallScope) -> Result:
        out = self._request("POST", "/perform", request_id=scope.request_id,
                            json_body={"action": action, "params": params,
                                       "scope": scope.to_json()})
        if "binary_b64" in out:
            try:
                data = base64.b64decode(out["binary_b64"], validate=True)
            except (ValueError, TypeError) as exc:
                raise AdapterError(502, "plugin returned malformed binary") from exc
            return Result(binary=data, mime=out.get("mime") or "application/octet-stream")
        return Result(data=out.get("data"))

    def connect_start(self, enabled_plugins: list[str]) -> dict:
        return self._request("POST", "/connect/start",
                             json_body={"enabled_plugins": list(enabled_plugins)})

    def connect_finish(self, code, state, installation_id) -> dict:
        return self._request("POST", "/connect/finish", json_body={
            "code": code, "state": state, "installation_id": installation_id})

    def connect_qr(self) -> bytes:
        return self._request("GET", "/connect/qr.png", raw=True)

    def disconnect(self) -> dict:
        return self._request("POST", "/disconnect")


def request(base_url: str, token: str, method: str, path: str, *, json_body: Any = None,
            plugin_id: str | None = None, request_id: str | None = None, raw: bool = False,
            timeout: float = 30.0, factory: ClientFactory | None = None):
    """One plugin API call with the error contract applied. Shared by
    RemoteAdapter and the registry's manifest discovery."""
    body = None
    if json_body is not None:
        try:
            body = encode_json(json_body)
        except (TypeError, ValueError, RecursionError) as exc:
            # Nothing was sent: a plain 400, not an unhandled 500 (which
            # would also leave a write's budget reservation dangling).
            raise AdapterError(400, UNENCODABLE) from exc
    headers = {"X-Plugin-Token": token}
    if plugin_id:
        headers["X-Plugin-Id"] = plugin_id
    if request_id:
        headers["X-Request-Id"] = request_id
    if body is not None:
        headers["Content-Type"] = "application/json"
    try:
        with (factory or _default_client)(base_url, headers, timeout) as client:
            resp = client.request(method, path, content=body)
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        # Never reached the plugin: definitely not performed, retryable.
        raise AdapterError(503, f"plugin service unreachable ({type(exc).__name__})") from exc
    except httpx.HTTPError as exc:
        # The request may already be on the wire (read timeout, reset): the
        # action may have happened. Unknown outcome, never auto-retried.
        raise AdapterError(502, f"plugin call failed with unknown outcome "
                                f"({type(exc).__name__})") from exc
    if resp.status_code >= 400:
        raise AdapterError(_map_status(resp.status_code), _error_text(resp))
    if raw:
        return resp.content
    try:
        body = resp.json()
    except ValueError as exc:
        raise AdapterError(502, "plugin returned a non-JSON response") from exc
    if not isinstance(body, dict):
        raise AdapterError(502, "plugin returned an unexpected response shape")
    return body


def _map_status(status: int) -> int:
    # 4xx and the two contract statuses pass through. Any other 5xx (500,
    # 504, ...) cannot promise "not performed", so it is an unknown outcome.
    if 400 <= status <= 499 or status in (502, 503):
        return status
    return 502


def _error_text(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        msg = body.get("error") if isinstance(body, dict) else None
    except ValueError:
        msg = None
    return str(msg or f"plugin answered {resp.status_code}")[:500]
