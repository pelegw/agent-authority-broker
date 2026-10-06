# Delegation

An agent can mint a child key for a sub-agent. The child's authority comes out
of the agent's own authority, with no human in the loop. This document covers
these topics:

- How the broker builds a chain.
- Why a chain can only narrow.
- What the owner sees.
- How revocation travels down a chain.

The code is in these files:

- `broker/broker/services/delegation.py`: the logic.
- `routers/delegations.py`: REST.
- `mcp_generic.py`: the same three operations as MCP tools.
- `services/admin.py::key_tree`: the owner's view.

`docs/grant-algebra.md` describes the algebra underneath.

## The three operations

| REST | MCP tool | What |
|---|---|---|
| `POST /v1/delegations` | `delegate` | Mint a child key. Returns its plaintext once. |
| `GET /v1/delegations` | `list_my_delegations` | The caller's direct children: status, role, expiry, grants. |
| `POST /v1/delegations/{key_id}/revoke` | `revoke_delegation` | Revoke a key below the caller, and everything below it. |

`POST /v1/delegations` takes this body:

```json
{"name": "helper",
 "capabilities": [{"target": "whatsapp", "actions": ["read_messages"],
                   "selector": {"chat": ["972501234567@s.whatsapp.net"]}}],
 "expires_in_hours": 8, "reason": "summarize one chat",
 "role": "read-only", "rate_per_min": 10,
 "denies": {"whatsapp": {"chat": ["120363000000000000@g.us"]}}}
```

It answers `201 {"key_id", "name", "key", "expires_at", "role",
"rate_per_min", "capabilities"}` with `Cache-Control: no-store`. The body must
have `name` and `capabilities`. The other fields are optional. `role`,
`rate_per_min` and the lifetime default to the caller's own.

**`role` is the child's ceiling.** It never grants anything: the child's
capabilities are the only grant. It caps every capability below it, per side
effect:

- `read-only`: the broker denies writes and destructive actions.
- `read-draft`: writes and destructive actions run as drafts, even where a
  capability says direct.
- `read-act`: destructive actions run as drafts.
- `full`: caps nothing.

A delegated key's ceiling defaults to its caller's role, never to the owner
default (`full` for keys the owner creates). It can never exceed the caller's
role. The caller's own ceiling, and the ceiling of every ancestor, also cap
the child. `get_my_access` reports the lowest role along the chain as
`ceiling`, with `effective_mode` on each action it lowers.

**A capability without `mode` asks for draft.** This applies to every
agent-originated capability: `delegate` and `request_permission` alike, over
REST or MCP. The broker reads such a capability as `"mode": "draft"`. Its
writes and destructive actions queue for a human, even if the caller can act
directly. Reads stay direct, because the algebra splits them out. An agent
must ask for autonomy explicitly with `"mode": "direct"`, so a forgotten field
never buys it. A draft capability cannot reach a write that the manifest
cannot draft (`modes: [direct]`). So a request for such a write without a mode
gets `400 invalid_capabilities`. Its hint says to set `"mode": "direct"`.
Owner-authored grants do not change. These come from creating or editing a
key in the console or admin API. There, a mode-less capability stays
`direct`. See `services/agent.normalize_request`.

## How a chain is built

A delegation writes two kinds of rows, and nothing else:

1. **The child key** (`api_keys`, `parent_key_id` = the caller,
   `created_by = delegation`). `auth.create_key` writes it. That function
   rejects a role, rate or expiry above the parent's, and a depth past
   `max_delegation_depth` (3). The name is `<caller's name>/<name>`. Thus a
   sub-agent's key can never pose as another key in approval cards, decisions
   or the audit log. Also, an agent cannot probe the global key names. The
   broker stores the child's own `denies` as given. At every authentication,
   it merges them with the denies of every ancestor. Thus the effective denies
   of a child are always a superset of its parent's (property 11).
2. **One `kind = delegation` grant per contributing parent grant.** The broker
   takes each live grant of the caller in turn. It narrows the requested
   capabilities against what the caller can use through that grant right now.
   The child's
   role bounds the result. The broker inserts every non-empty result as a
   child of that grant, through `store.insert_child_grant`. That function
   accepts only a `NarrowedCapabilities`, and only `narrow()` can build one.
   It also checks `grant_le(child, parent)` again against the stored parent
   row, in the same transaction.

"What the caller can use right now" is the chain meet of each grant, with
three limits:

- The owner ceiling (enabled and connected plugins) intersects it.
- The role of every key in the caller's chain bounds it.
- The caller's denies come off it.

That is stricter than narrowing against the stored rows, and it keeps the
answer honest. The broker creates a delegation exactly as asked, or rejects it
with `400 clipped`. The rejection lists `clipped`: the requested capabilities
that did not fit. It also lists `allowed`: what the caller can give. Before
the broker returns `allowed`, it removes hidden and denied ids from it, and
ids under a hidden folder. Thus a rejection never names a resource the caller
cannot see. The broker writes nothing for a rejected request.

The caller's authority can change between the check and the insert, for
example when a grant gets revoked in between. Then the store rejects the
insert, and the broker undoes the delegation:

- With no grant written, the broker deletes the key row. Nobody has seen its
  secret.
- Otherwise, the broker disables the key and revokes the grants already
  written.

The caller gets `409 conflict`.

Other rejections:

- `400 exceeds_parent` (with `field` and `max`): a role, rate or lifetime
  above the caller's.
- `400 depth_exceeded`: at the depth limit. The MCP tool list does not even
  show the `delegate` tool to such a key, and `get_my_access` reports
  `can_delegate: false`.
- `409 name_taken`.
- `409 too_many_delegations`.
- `429 rate_limited`.
- `400 invalid_name`, `invalid_role`, `invalid_denies`, `invalid_capabilities`.

## Why it can only narrow

Nothing trusts the stored child rows. On every call, the broker calculates the
child's effective set again (`authority/effective.py`):

- It walks the chains of the child's grants to the root and meets them link by
  link. Thus a root grant edited narrower, or any revoked or expired link,
  shrinks or empties the child on the next call.
- The ceiling (role) of every key in its key chain caps it. Thus a lower
  ceiling on a parent caps the whole subtree at once, whatever the child's own
  row says.
- The owner ceiling drops disabled or disconnected plugins.
- The broker subtracts the merged denies.

Two hypothesis properties check this through the agent surfaces
(`tests/test_delegation_properties.py`). They run after every step of random
sequences of `delegate`, `request_permission` and `revoke_delegation` (REST or
MCP). The sequences also contain the owner's approvals, revocations, root
edits, role changes and deny edits:

- **10.** A delegated key's effective set never exceeds its parent's. A key
  never authenticates while its parent does not. A new delegation never
  exceeds what the agent asked for. `revoke_delegation` succeeds exactly for a
  strict ancestor and kills the whole subtree.
- **11.** Every key's merged denies include its parent's.

`tests/authority/test_grant_properties.py` holds the library-level versions of
the same properties.

A delegated key can also ask the owner for more with `request_permission`. The
broker narrows the request against the grants of its parent key, never the
ceiling. Thus an approval can never give it more than the parent holds, and
the chain meet bounds it again at every call.

## What the owner sees

`GET /v1/admin/keys/tree` returns every key as a forest: root keys with their
delegated children nested. Each node carries the key's row (name, role, rate,
expiry, denies, `created_by`, `parent_key_id`, last use), plus these fields:

- `status`: the key's own row, `active` | `disabled` | `expired`.
- `live`: whether the key would authenticate right now. That is, the key and
  every ancestor are active, and the key is within the depth limit. A
  grandchild of a revoked key shows `status: active, live: false`.
- `depth`.
- Its `grants`, with `kind`, `status`, `parent_grant_id`, and
  `decided_via: agent` for grants an agent revoked.
- `children`.

Some keys have no path from any root: their parent row is missing, or they
are in a corrupted parent loop. The tree shows such a key at the top level
with `orphan: true`. That key cannot authenticate, because its chain is
broken.

The audit log records every delegation and revocation under the acting key's
name (`delegation.create`, `delegation.revoke`). The record has the child's
name, role, rate, expiry and grant ids. No log line carries the plaintext key.
No human approves a delegation, so the owner's control comes after the fact.
The owner can do these things:

- Disable or edit any key in the tree.
- Revoke any grant.
- Lower any key's ceiling (role).

## Revocation propagation

Revocation writes to one key only. Everything below it stops through the chain
walk, with no cascade writes:

| Event | Effect on descendants |
|---|---|
| An agent revokes a child (`revoke_delegation`) | The child key is disabled and its grants revoked (`decided_via: agent`). Every key below it gets 401 at its next call: `authenticate_bearer` walks the whole key chain. |
| The owner disables any key | The same: that key and its whole subtree get 401. |
| The owner revokes, or a grant expires | Every grant chained below it evaluates to nothing (`chain_meet` needs every link live): 403 `out_of_grant`, while the keys still authenticate. |
| The owner narrows a root grant | Every descendant shrinks to the new bound on its next call. |
| The owner lowers a key's ceiling (role) | Every descendant is capped by it on its next call; its `get_my_access` shows the lower `ceiling`. |
| A plugin is disabled or disconnected | No key keeps any capability for it. |

`revoke_delegation` reaches descendants only. A key can revoke its children,
grandchildren and so on. It can never revoke itself, an ancestor, a sibling,
or another lineage. All of those get a plain `404`, indistinguishable from a
key id that does not exist. An agent can only ever move a grant to `revoked`.
The store rejects `decided_via: agent` for any other transition, and for the
creation of a grant.

## Limits and settings

- `max_delegation_depth` (default 3, console-editable 0-10, see
  `docs/configuration.md`) is a per-call read. The broker reads it through
  `auth.max_delegation_depth()` -> `runtime_settings()`. That one accessor
  serves authentication, key creation, `delegate`, `get_my_access` and the MCP
  tool list. A higher limit lets deeper keys delegate at once. A lower limit
  makes every key deeper than the new limit fail authentication on its next
  request. The broker writes nothing to their rows, so a higher limit later
  restores them.
- A delegation can ask for at most 87600 hours (10 years), and never beyond
  the caller's own expiry. Its grants expire with the key.
- Every `delegate` attempt, granted or rejected, spends one call of the
  caller's per-minute rate. That is the same budget as its actions. Past the
  budget, the answer is `429 rate_limited`.
- A key can hold at most `max_live_delegations` live direct children (default
  25, console-editable 1-200). Past that, the answer is
  `409 too_many_delegations` until one child expires or a revocation ends it.
  With the depth limit, that keeps any tree bounded without a human in the
  loop.
- An agent's `denies` must name registered targets and their declared resource
  kinds, at most 500 ids. The plugin normalizes the ids, as for the owner's key
  editor (`services/deny_input.py`).
