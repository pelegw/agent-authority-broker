# Plugin API

The internal HTTP contract between the broker and a plugin service. Every
plugin runs in its own container (docs/architecture.md, section 2); the
broker's `RemoteAdapter` (`broker/broker/plugins/adapter.py`) is the client,
and the `aab_plugin_runtime` package (`plugin-runtime/`) is the server that
hosts any Python adapter. The broker holds no target credential: it sends
*requirements* per call and the plugin mints whatever token it needs.

A plugin service may host several plugin ids (plugin-google serves `gmail`,
`gcal` and `gdrive`, which share one OAuth credential). Env on the broker is
keyed by **service**: `PLUGIN_URL_<SERVICE>` and `PLUGIN_TOKEN_<SERVICE>`.

## Headers

| Header | Direction | Meaning |
|---|---|---|
| `X-Plugin-Token` | broker → plugin, **every** request | The service's shared token (`PLUGIN_TOKEN_<SERVICE>`). Compared in constant time (`hmac.compare_digest`). Missing or wrong: `401 {"error": "unauthorized"}`. A runtime started with an empty token refuses to boot. |
| `X-Plugin-Id` | broker → plugin | Which hosted plugin the request is for. Optional when the service hosts exactly one; otherwise missing = 400, unknown = 404. |
| `X-Request-Id` | both | Links the plugin call to the broker's decision record (`decisions.request_id`). Echoed back on the response; also put into `scope.request_id` for `/perform` when the scope lacks one. |

The token never appears in a response, an error message, a log line, or a
`repr` on either side.

## Endpoints

| Method + path | Body | Response (200) | Notes |
|---|---|---|---|
| `GET /manifests` | | `{"manifests": [manifest, ...]}` | Every manifest the service hosts, as plain JSON. The broker pins each against its vendored copy (`broker/broker/targets/<id>/manifest.yaml`): id and version must match, and the **vendored** copy is used thereafter. A mismatch refuses the plugin and is audited (`plugin.refused`). |
| `GET /status` | | `{"connected": bool, "healthy": bool, "enforcement"?: "target"\|"proxy", "connection"?: {...}, ...}` | Stored by the broker as `plugins.last_health`; `connected` feeds the owner ceiling. `enforcement: "proxy"` means the live connection cannot enforce anything at the target right now (e.g. a GitHub PAT fallback): every dimension is reported `proxy`. Unreadable secrets answer `{"connected": false, "healthy": false, "health": "reconnect required"}`. |
| `POST /configure` | `{"config": {...}, "secrets": {name: value \| null}}` | `{"ok": true}` | `config` = the non-secret fields (the broker stores those itself). `secrets` are written to the plugin's own encrypted store (`null`/`""` deletes) and are **never returned** by any endpoint. A secret field the manifest marks `shared: true` is written to the connection's shared slot (`connection.shared`, e.g. `google`) instead of the plugin id's, so one value serves every plugin of the service and is stored once; the runtime refuses to boot if the adapter's connection has no such slot. |
| `POST /normalize` | `{"kind", "value"}` | `{"id": "..."}` | Canonical id for a resource (e.g. a phone number to a JID). Bad input: 400. |
| `POST /resolve` | `{"kind", "query", "limit", "relation"?}` | `{"items": [{"id", "label", "kind"}]}` | Name search for pickers. With `"relation": "ancestors"` and `query` = a resource id, returns `{"ancestors": [id, ...]}` nearest first; this backs `subtree` narrowings. |
| `POST /label` | `{"kind", "ids": [...]}` | `{"labels": {id: label}}` | Batch labels for approval cards and the console. Unknown ids are simply absent. |
| `POST /perform` | `{"action", "params", "scope": CallScope}` | `{"data": ...}` or `{"binary_b64": "...", "mime": "..."}` | See below. |
| `POST /connect/start` | `{"enabled_plugins": [...], "redirect_uri"?}` | `{"kind": "oauth"\|"install"\|"qr"\|"none", "url"?, "state"?}` | The plugin generates and stores any `state` nonce itself (10 min TTL, single use). Google builds the consent URL with scopes = the union of the enabled plugins' `target_permissions`. `redirect_uri` is computed by the broker (public mode: `https://<SITE_DOMAIN>/oauth/callback/<service>`, never from a Host header; local mode: `http://<request host>/oauth/callback/<service>`); an OAuth plugin stores it beside the state nonce and reuses it for the code exchange. The runtime passes it only to a connection whose `start()` takes a `redirect_uri` keyword. |
| `GET /connect/qr.png` | | `image/png` | WhatsApp only (proxied from the sidecar). `Cache-Control: no-store`. |
| `POST /connect/finish` | `{"code"?, "state"?, "installation_id"?}` | plugin-defined JSON | The plugin checks `state`, exchanges the code with its own client secret, and stores the credential in its own volume. The broker relays the code once and records it nowhere. |
| `POST /disconnect` | | plugin-defined JSON | The plugin wipes its stored credential. |

## CallScope

Sent with every `/perform`. It is everything the plugin needs to stay inside
the broker's decision:

```json
{
  "request_id": "5f0c...e1",
  "visibility": {
    "room":   {"deny": ["r2"], "allow_only": ["r1", "r3"]},
    "folder": {"deny": [],     "allow_only": ["a"]},
    "item":   {"deny": ["i9"], "allow_only": null}
  },
  "constraints": {"window_days": 7, "attachments": false},
  "credential":  {"permissions": {"items": "write"},
                  "resources": {"repo": ["owner/name"]}}
}
```

- `visibility` is keyed by resource kind (or, for a pattern dimension with no
  resource kind, by the dimension name). `deny` = the owner's hidden
  resources ∪ the key's merged denies. `allow_only` = the covering
  capability's explicit selector for that kind, or `null` for unrestricted.
  For a `subtree` kind the ids are roots: a resource is inside if it or any
  ancestor is listed, and hiding a folder hides its whole subtree. **Deny
  wins.** The plugin must never return, act on, or acknowledge a denied
  resource, and a get on one is a `404` indistinguishable from a missing
  resource.
- `constraints` are the covering capability's constraints that apply to this
  action (manifest `applies_to`).
- `credential` is a set of *requirements*, never a credential:
  `permissions` = the action's `target_permissions`, `resources` = the
  target-enforced selector dimensions. The plugin's connection mints a token
  restricted to exactly that (GitHub installation token; Google access token
  per scope set) and may cache it by the exact tuple.

## Results

- JSON: `{"data": <anything JSON>}`. Rows that name a resource should carry
  `"resource_ref": {"kind": "...", "id": "..."}`: the broker drops every such
  row the scope does not allow (belt and braces over the plugin's own
  filtering), and a single returned object that is itself not allowed
  becomes a 404. A malformed `resource_ref` is treated as not allowed.
- Binary (`returns: binary` in the manifest): `{"binary_b64": "...", "mime": "..."}`.
- `long_poll` actions return a JSON object with a cursor and a list
  (e.g. `{"cursor": 7, "items": []}`); every list empty means "nothing new",
  which is what the broker's `?wait=` loop waits on.

## Errors and the 503 / 502 contract

Error bodies are `{"error": "<message>"}`.

| Where | What happened | Status the broker acts on | Broker behaviour |
|---|---|---|---|
| plugin | bad input, forbidden by a constraint, conflict | the plugin's 4xx (passthrough) | reservation released; agent sees the status (every 404 is rewritten to the one `not found` body, so hidden and missing look the same) |
| plugin | **not performed**, safe to retry (target down, not paired, secrets unreadable) | **503** | reservation released; a queued action returns to `pending`/`scheduled` and is retried |
| plugin | the call may have reached the target, outcome unknown | **502** | reservation **kept** for 24 h; a queued action becomes `failed` with the result recorded and is **never** retried automatically |
| plugin | unexpected exception in the adapter | 502 (runtime maps it; body has no internals) | as 502 |
| plugin | any other 5xx (500, 504, ...) | 502 | as 502: nothing promises "not performed" |
| broker | connection refused / connect timeout (never reached the plugin) | **503** | as 503 |
| broker | read timeout, reset, broken or non-JSON response after sending | **502** | as 502 |

The broker's timeout for plugin calls is `PLUGIN_TIMEOUT_SECONDS` (30 s).

## Implementing a plugin

A plugin is a Python object with `manifest` (the manifest as a dict),
`connection` (or `None`), and `configure`, `status`, `normalize`,
`resolve`, `label`, `perform` (and optionally `ancestors`), raising
`aab_plugin_runtime.AdapterError(status, message)` to refuse. Its
connection implements `start` (optionally with a `redirect_uri` keyword), `finish`, `qr_png`, `disconnect`, `status`,
`mint`. Adapters and connections that persist credentials they obtain
define `bind_secrets(slot)` and receive a read-write handle on their own
encrypted slot. Serve it with:

```python
from aab_plugin_runtime import from_env
app = from_env([MyAdapter()])     # PLUGIN_TOKEN, PLUGIN_SECRETS_KEY, PLUGIN_SECRETS_DIR
```

The secret store refuses to boot when encrypted data exists but
`PLUGIN_SECRETS_KEY` is missing; the default directory is under the running
user's home, so the container can run as a non-root user. The broker's test
plugin `broker/tests/fixtures/echo/adapter.py` is a complete worked example.
