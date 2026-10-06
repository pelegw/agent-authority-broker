# Plugin API

This document is the internal HTTP contract between the broker and a plugin
service. Every plugin runs in its own container (docs/architecture.md,
section 2). The `RemoteAdapter` of the broker
(`broker/broker/plugins/adapter.py`) is the client. The `aab_plugin_runtime`
package (`plugin-runtime/`) is the server for any Python adapter. The broker
holds no target credential. It sends *requirements* with each call, and the
plugin mints the token that it needs.

A plugin service can serve several plugin ids. For example, plugin-google
serves `gmail`, `gcal` and `gdrive`, which share one OAuth credential. The
environment of the broker names each entry by **service**:
`PLUGIN_URL_<SERVICE>` and `PLUGIN_TOKEN_<SERVICE>`.

## Headers

| Header | Direction | Meaning |
|---|---|---|
| `X-Plugin-Token` | broker → plugin, **every** request | The service's shared token (`PLUGIN_TOKEN_<SERVICE>`). Compared in constant time (`hmac.compare_digest`). Missing or wrong: `401 {"error": "unauthorized"}`. A runtime with an empty token does not start. |
| `X-Plugin-Id` | broker → plugin | The plugin of the service that the request is for. Optional when the service serves exactly one plugin; otherwise missing = 400, unknown = 404. |
| `X-Request-Id` | both | Links the plugin call to the broker's decision record (`decisions.request_id`). Echoed back on the response; also put into `scope.request_id` for `/perform` when the scope lacks one. |

The token never appears in a response, an error message, a log line, or a
`repr` on either side.

## Endpoints

| Method + path | Body | Response (200) | Notes |
|---|---|---|---|
| `GET /manifests` | | `{"manifests": [manifest, ...]}` | Every manifest that the service serves, as plain JSON. The broker pins each against its vendored copy (`broker/broker/targets/<id>/manifest.yaml`): id and version must match, and the broker then uses the **vendored** copy. On a mismatch, the broker rejects the plugin and writes an audit entry (`plugin.refused`). |
| `GET /status` | | `{"connected": bool, "healthy": bool, "enforcement"?: "target"\|"mixed"\|"proxy", "connection"?: {...}, ...}` | The broker stores it as `plugins.last_health`. `connected` feeds the owner ceiling. `enforcement: "proxy"` means that the live connection cannot enforce anything at the target now, for example a GitHub PAT fallback. Then the broker reports every dimension as `proxy`. `"target"` or `"mixed"` means that the target narrows the live credential, so each dimension gets what the manifest declares. A plugin whose manifest claims `target` **must** report one of these. The broker reads a status without `enforcement`, or with any other value, as `proxy`. A refresh that fails keeps the last reported value and records `healthy: false`. Thus an outage never turns proxy into target. Unreadable secrets answer `{"connected": false, "healthy": false, "health": "reconnect required"}`. |
| `POST /configure` | `{"config": {...}, "secrets": {name: value \| null}}` | `{"ok": true}` | `config` = the non-secret fields. The broker stores those itself. The plugin writes `secrets` to its own encrypted store (`null`/`""` deletes), and **no endpoint ever returns them**. The plugin writes a secret field that the manifest marks `shared: true` to the shared slot of the connection (`connection.shared`, for example `google`), not to the slot of the plugin id. Thus one value serves every plugin of the service, and the plugin stores it once. The runtime does not start if the connection of the adapter has no such slot. |
| `POST /normalize` | `{"kind", "value"}` | `{"id": "..."}` | Canonical id for a resource (for example, a phone number to a JID). Bad input: 400. |
| `POST /resolve` | `{"kind", "query", "limit", "relation"?}` | `{"items": [{"id", "label", "kind"}]}` | Name search for pickers. With `"relation": "ancestors"` and `query` = a resource id, returns `{"ancestors": [id, ...]}` nearest first; this backs `subtree` narrowings. |
| `POST /label` | `{"kind", "ids": [...]}` | `{"labels": {id: label}}` | Batch labels for approval cards and the console. Unknown ids are simply absent. |
| `POST /perform` | `{"action", "params", "scope": CallScope}` | `{"data": ...}` or `{"binary_b64": "...", "mime": "..."}` | See below. |
| `POST /connect/start` | `{"enabled_plugins": [...], "redirect_uri"?}` | `{"kind": "oauth"\|"install"\|"qr"\|"none", "url"?, "state"?}` | The plugin generates and stores any `state` nonce itself (10 min TTL, single use). Google builds the consent URL with scopes = the union of the enabled plugins' `target_permissions`. The broker calculates `redirect_uri` (public mode: `https://<SITE_DOMAIN>/oauth/callback/<service>`, never from a `Host` header; local mode: `http://<request host>/oauth/callback/<service>`). An OAuth plugin stores it beside the state nonce and uses it again for the code exchange. The runtime passes it only to a connection whose `start()` takes a `redirect_uri` keyword. |
| `GET /connect/qr.png` | | `image/png` | WhatsApp only (proxied from the sidecar). `Cache-Control: no-store`. |
| `POST /connect/finish` | `{"code"?, "state"?, "installation_id"?}` | plugin-defined JSON | The plugin checks `state`, exchanges the code with its own client secret, and stores the credential in its own volume. The broker sends the code on one time and records it nowhere. |
| `POST /disconnect` | | plugin-defined JSON | The plugin wipes its stored credential. |

## CallScope

The broker sends a CallScope with every `/perform`. It holds everything that
the plugin needs to stay inside the decision of the broker:

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

- `visibility` has one entry for each resource kind. For a pattern dimension
  with no resource kind, the entry has the dimension name. `deny` = the
  hidden resources of the owner ∪ the merged denies of the key. `allow_only`
  = the explicit selector of the covering capability for that kind, or
  `null` for unrestricted. For a `subtree` kind, the ids are roots. A
  resource is inside if the list contains it or one of its ancestors.
  Hiding a folder hides its whole subtree. **Deny wins.** The plugin must
  never return, act on, or acknowledge a denied resource. A get on a denied
  resource is a `404`, indistinguishable from a missing resource.
- `constraints` are the constraints of the covering capability that apply to
  this action (manifest `applies_to`).
- `credential` is a set of *requirements*, never a credential. `permissions`
  = the `target_permissions` of the action. `resources` = the
  target-enforced selector dimensions. The connection of the plugin mints a
  token for exactly those requirements: a GitHub installation token, or a
  Google access token for each scope set. It can cache the token by the
  exact tuple.

## Results

- JSON: `{"data": <anything JSON>}`. A row that names a resource must carry
  `"resource_ref": {"kind": "...", "id": "..."}`. The broker drops every such
  row that the scope does not permit. This is belt and braces over the
  filtering of the plugin. A single returned object that the scope does not
  permit becomes a 404. The broker treats a malformed `resource_ref` as not
  permitted.
- Binary (`returns: binary` in the manifest): `{"binary_b64": "...", "mime": "..."}`.
- A `long_poll` action returns a JSON object with a cursor and a list, for
  example `{"cursor": 7, "items": []}`. Every list empty means "nothing new".
  The `?wait=` loop of the broker waits for that. An action that declares a
  `cursor` param, called without one, is a **bootstrap** ("start from now").
  The broker answers it at once, whatever `wait` says.
- Binary results reach REST callers as `X-Content-Type-Options: nosniff`
  attachments. The broker names each attachment after the most specific id
  of the call. That is the first required string param that is not the
  selector, else the resource id.

## Errors and the 503 / 502 contract

Error bodies are `{"error": "<message>"}`.

| Where | What happened | Status the broker acts on | Broker behaviour |
|---|---|---|---|
| plugin | bad input, forbidden by a constraint, conflict | the plugin's 4xx (passthrough) | reservation released; agent sees the status (every 404 is rewritten to the one `not found` body, so hidden and missing look the same) |
| plugin | **did nothing**, safe to retry (target down, not paired, secrets unreadable) | **503** | reservation released; a queued action returns to `pending`/`scheduled` and is retried |
| plugin | the call possibly reached the target, outcome unknown | **502** | reservation **kept** for 24 h; a queued action becomes `failed` with the result recorded and is **never** retried automatically |
| plugin | unexpected exception in the adapter | 502 (runtime maps it; body has no internals) | as 502 |
| plugin | any other 5xx (500, 504, ...) | 502 | as 502: no status promises "did nothing" |
| broker | agent params that are not UTF-8 JSON (lone surrogate, NaN, Infinity) | **400** `invalid_params` | recorded deny before evaluation; no plugin call, no reservation (the transport rejects such a body too) |
| broker | connection refused / connect timeout (never reached the plugin) | **503** | as 503 |
| broker | read timeout, reset, broken or non-JSON response after sending | **502** | as 502 |

The broker's timeout for plugin calls is `PLUGIN_TIMEOUT_SECONDS` (30 s).

## Implementing a plugin

A plugin is a Python object with these attributes and methods:

- `manifest`: the manifest as a dict.
- `connection`: a connection object, or `None`.
- `configure`, `status`, `normalize`, `resolve`, `label` and `perform`.
- Optionally, `ancestors`.

To reject a call, the adapter raises
`aab_plugin_runtime.AdapterError(status, message)`. Its connection implements
`start` (optionally with a `redirect_uri` keyword), `finish`, `qr_png`,
`disconnect`, `status` and `mint`. Adapters and connections that persist the
credentials they get define `bind_secrets(slot)`. They receive a read-write
handle on their own encrypted slot. Serve the adapter like this:

```python
from aab_plugin_runtime import from_env
app = from_env([MyAdapter()])     # PLUGIN_TOKEN, PLUGIN_SECRETS_KEY, PLUGIN_SECRETS_DIR
```

If encrypted data exists but `PLUGIN_SECRETS_KEY` is missing, the secret
store stops the runtime from starting. The default directory is under the
home directory of the running user, so the container can run as a non-root
user. The test plugin of the broker, `broker/tests/fixtures/echo/adapter.py`,
is a complete worked example.
