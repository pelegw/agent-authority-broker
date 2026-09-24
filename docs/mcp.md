# MCP surface

REST is the main agent surface. MCP is a second view over the same registry
and the same dispatch function (`engine.perform`), served at `/mcp` as
stateless streamable HTTP. Code: `broker/broker/mcp_server.py` (transport,
auth, handlers), `mcp_tools.py` (plugin action tools), `mcp_generic.py`
(the generic tools). `tests/test_parity.py` checks that the two surfaces
agree.

## Transport

- The official SDK's low-level `mcp.server.lowlevel.Server` runs behind
  `StreamableHTTPSessionManager(stateless=True, json_response=True)`. FastMCP
  is not used: its decorator registry fixes the tool list at import time.
- `main.BrokerApp` sends `/mcp` and `/mcp/...` to the MCP app and everything
  else to FastAPI. It does not use a Starlette `Mount`, because a Mount would
  307-redirect bare `/mcp` and MCP clients don't reliably follow redirects on
  POST. `OriginGuardMiddleware` wraps `BrokerApp`, so the origin secret and the
  trusted client IP apply to MCP too.
- The app lifespan runs a new session manager each time it starts
  (`mcp_server.run_session_manager()`), because a manager's `run()` only works
  once per instance. Outside the lifespan, `/mcp` answers 503.
- DNS-rebinding protection is on. The server only accepts `Host` headers
  listed in `MCP_ALLOWED_HOSTS` (read when the lifespan starts), and rejects
  a foreign `Origin`. A wrong host gets 421 and a wrong origin gets 403.

## Authentication

`MCPAuthMiddleware` runs before the SDK sees the request. It turns
`Authorization: Bearer aab_…` into an `AuthContext` through
`agent_auth.authenticate`, the same function the REST dependency uses. That
call runs on the threadpool because it reads SQLite. The result goes into the
`CURRENT_AUTH` ContextVar, and the per-request server task inherits it. A
missing, invalid or disabled key gets 401 with
`{"error", "code": "unauthorized"}`. An admin token is not an agent key and
also gets 401. If a handler ever runs without an auth context, it fails
closed.

## How tools are derived

`tools/list` is computed **per request**:

1. The generic tools (below) always come first.
2. Then one tool for every action that this key can reach on an enabled
   plugin. "Reachable" means `services/agent.reachable_actions(auth)`, the
   same function that REST `/v1/targets` and `/v1/me/openapi.json` use. An
   action is reachable when one of the key's effective capabilities
   (`P ∩ G ∩ R`, evaluated live) names it and the capability's mode can run
   it (`policy.run_mode`). For example, a draft-only authority cannot reach
   a write that cannot be drafted. Resource selectors are checked at call
   time, not at listing time.

Each action tool has:

- **name** `<plugin>_<action>`. The canonical id is `plugin.action`, but MCP
  names can't contain `.`. Plugin ids match `^[a-z][a-z0-9]*$` (no
  underscore), so the name splits back unambiguously at the first `_`. If a
  plugin tool name ever collides with a generic tool name, the generic tool
  wins. That plugin action stays reachable over REST.
- **inputSchema**: the action's params schema, flat, with
  `additionalProperties: false`, plus the call controls that the REST body
  carries at top level:
  - `as_draft` for writes and destructive actions whose modes include
    `draft`;
  - `run_at` / `delay_seconds` for `schedulable` actions;
  - `note` for anything that can end up queued.

  If a manifest param has the same name as a control, the param wins and
  that control isn't offered for that action. A control name that is not a
  param is always treated as a control, even where it isn't advertised. The
  engine then refuses an unsupported one precisely (`draft_unsupported`,
  `not_schedulable`), the same way REST does.
- **description**: the action's `doc`, plus hints:
  - writes may return `{"status": "pending_approval", "action_id"}`, which is
    normal, not an error;
  - schedulable actions may return `{"status": "scheduled", "action_id"}`;
  - binary actions return base64 content;
  - long-poll actions return immediately over MCP. Waiting is REST-only
    (`GET …?wait=N`), because a blocking tool call trips host timeouts.
- **annotations**: `readOnlyHint` for reads, `destructiveHint` for
  destructive actions.

## Calls

`tools/call` runs `mcp_tools.dispatch` on the threadpool. It never runs on the
event loop.

- A generic tool validates its arguments with a pydantic model (extra keys are
  refused) and calls the same `services/agent.py` function as its REST route.
- A plugin tool splits the arguments into params and controls and calls
  `engine.perform`. The engine evaluates everything again, so a stale tool
  name gets the same answer as REST: 404 for a disabled plugin, 403 for a
  revoked grant. Every call writes a decision record, the same as over REST.
- Results:
  - JSON data, and the 202 envelope, are returned as one compact JSON text
    block. This is byte-for-byte the REST body.
  - `returns: binary` becomes an `ImageContent` for `image/*`, or otherwise an
    `EmbeddedResource` with a `BlobResourceContents` (base64 + `mimeType`,
    uri `broker://result/<plugin>/<action>`).
  - A refusal (`PolicyError`) comes back as `isError: true` with the same
    `{"error", "code", "hint"?}` JSON as the REST error body (plus the same
    extras, such as `clipped`/`allowed` or `grant_id`/`budget`). Invalid
    arguments get `invalid_request` without echoing the input. An unexpected
    exception is logged server-side and returned as
    `{"error": "internal error", "code": "internal"}`, never as a trace or an
    internal message.
- The SDK's own input validation is off (`validate_input=False`). It checks
  against a tool cache shared by every caller of the process-wide `Server`,
  which is wrong for a list that differs per caller. The engine and the
  generic tools' models validate strictly instead.

## Generic tools

| Tool | REST equivalent |
|---|---|
| `get_my_access` | `GET /v1/me` |
| `list_targets` | `GET /v1/targets` |
| `resolve_resource(target, kind, query, limit?)` | `GET /v1/targets/{t}/resolve?kind=&q=` |
| `request_permission(capabilities, reason?, expires_in_hours?)` | `POST /v1/permissions` |
| `get_permission_status(grant_id)` | `GET /v1/permissions/{id}` |
| `list_my_permissions(limit?, cursor?)` | `GET /v1/permissions` |
| `delegate(name, capabilities, expires_in_hours?, reason?, role?, rate_per_min?, denies?)` | `POST /v1/delegations` |
| `list_my_delegations` | `GET /v1/delegations` |
| `revoke_delegation(key_id)` | `POST /v1/delegations/{key_id}/revoke` |
| `get_action_status(action_id)` | `GET /v1/actions/{id}` |
| `list_my_actions(status?, limit?, cursor?)` | `GET /v1/actions` |
| `cancel_action(action_id)` | `DELETE /v1/actions/{id}` |

A generic tool can be listed only for some keys: `delegate` is left out of
the list for a key already at the delegation depth limit. That is an
advertisement, never the check: calling it anyway gets the same
`400 depth_exceeded` as REST, from the service. Delegation is described in
`docs/delegation.md`.

## Resources

One resource, `broker://skill` (`text/markdown`): the skill doc filtered to
the calling key, the same text as `GET /v1/me/skill` for the same `Host`.
It lists only the actions the key can reach and a "Your current
capabilities" block built from `get_my_access` (never a hidden resource or a
deny set). `resources/read` of any other URI is a JSON-RPC error; a failure
while rendering is logged server-side and returned as `internal error`.

## Deliberately absent

- **No approve or reject tool.** Approval is a human act and exists only on
  the admin plane (console, admin API, Telegram).
  `tests/test_no_admin_from_agent_paths.py` walks the import graph from
  `broker.mcp_server` and asserts that `services/admin.py`, `deps.py` and
  `identity/` are unreachable.
- **No `tools/list_changed` notifications.** Stateless HTTP has no session to
  push them to. Clients should list again, and call-time gating makes a stale
  list harmless.
- **No listing of hidden resources or deny sets**, the same as REST.
- **No long-poll over MCP** (see above).
- **No stateful sessions, resumption or event store.** Each request stands
  alone.

## Why the list is computed per request

Rule 5 of the design: the tool list is enabled plugins ∩ the caller's
effective capabilities, and it must change the moment either changes.
Examples: a plugin is disabled in the console, a grant is approved, revoked
or expires, a parent key is disabled. The broker has no session to notify,
so it recomputes on every `tools/list`. It also never trusts the list at call
time: `engine.perform` evaluates from scratch on every call.
