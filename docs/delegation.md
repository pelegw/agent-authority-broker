# Delegation

An agent can mint a child key for a sub-agent, carved out of its own
authority, with no human in the loop. This document covers how a chain is
built, why it can only narrow, what the owner sees, and how revocation
travels down a chain. Code: `broker/broker/services/delegation.py` (the
logic), `routers/delegations.py` (REST), `mcp_generic.py` (the same three
operations as MCP tools), `services/admin.py::key_tree` (the owner's view).
The algebra underneath is `docs/grant-algebra.md`.

## The three operations

| REST | MCP tool | What |
|---|---|---|
| `POST /v1/delegations` | `delegate` | Mint a child key. Returns its plaintext once. |
| `GET /v1/delegations` | `list_my_delegations` | The caller's direct children: status, role, expiry, grants. |
| `POST /v1/delegations/{key_id}/revoke` | `revoke_delegation` | Revoke a key below the caller, and everything below it. |

`POST /v1/delegations` takes

```json
{"name": "helper",
 "capabilities": [{"target": "whatsapp", "actions": ["read_messages"],
                   "selector": {"chat": ["972501234567@s.whatsapp.net"]}}],
 "expires_in_hours": 8, "reason": "summarize one chat",
 "role": "read-only", "rate_per_min": 10,
 "denies": {"whatsapp": {"chat": ["120363000000000000@g.us"]}}}
```

and answers `201 {"key_id", "name", "key", "expires_at", "role",
"rate_per_min", "capabilities"}` with `Cache-Control: no-store`. Only `name`
and `capabilities` are required; `role`, `rate_per_min` and the lifetime
default to the caller's own.

**A capability without `mode` asks for draft.** Every agent-originated
capability (`delegate` and `request_permission` alike, over REST or MCP)
that omits `mode` is read as `"mode": "draft"`: its writes and destructive
actions queue for a human even if the caller could act directly; reads stay
direct (the algebra splits them out). Autonomy has to be asked for
explicitly with `"mode": "direct"`, so a forgotten field never buys it. A
write the manifest cannot draft (`modes: [direct]`) would be unreachable in a
draft capability, so asking for one without a mode is a `400
invalid_capabilities` whose hint says to set `"mode": "direct"`. Owner-authored
grants (creating or editing a key in the console or admin API) are not
affected: a mode-less capability there stays `direct`.
(`services/agent.normalize_request`.)

## How a chain is built

A delegation writes two kinds of rows, and nothing else:

1. **The child key** (`api_keys`, `parent_key_id` = the caller,
   `created_by = delegation`), through `auth.create_key`, which refuses a role,
   rate or expiry above the parent's and a depth past `max_delegation_depth`
   (3). The name is `<caller's name>/<name>`, so a sub-agent's key can never
   pose as another key in approval cards, decisions or the audit log, and an
   agent cannot probe the global key names. The child's own `denies` are
   stored as given; at every authentication they are merged with every
   ancestor's, so a child's effective denies are always a superset of its
   parent's (property 11).
2. **One `kind = delegation` grant per contributing parent grant.** For each
   live grant of the caller, the requested capabilities are narrowed against
   what the caller can use through that grant right now, bounded by the
   child's role, and every non-empty result is inserted as a child of that
   grant through `store.insert_child_grant`. That function accepts only a
   `NarrowedCapabilities` (only `narrow()` can build one) and re-checks
   `grant_le(child, parent)` against the stored parent row in the same
   transaction.

"What the caller can use right now" is each grant's chain meet, intersected
with the owner ceiling (enabled and connected plugins), bounded by the role of
every key in the caller's chain and minus the caller's denies. That is
stricter than narrowing against the stored rows, and it is what keeps the
answer honest: a delegation is created exactly as asked, or refused with
`400 clipped` listing `clipped` (the requested capabilities that did not fit)
and `allowed` (what the caller could give). Hidden and denied ids (and ids
under a hidden folder) are removed from `allowed` before it is returned, so a
refusal never names a resource the caller cannot see. Nothing is written for
a refused request.

If the caller's authority changes between the check and the insert (a grant
revoked in between), the store refuses the insert and the delegation is
undone: with no grant written the key row is deleted (nobody has seen its
secret); otherwise the key is disabled and the grants already written are
revoked. The caller gets `409 conflict`.

Other refusals: `400 exceeds_parent` (with `field` and `max`) for a role,
rate or lifetime above the caller's; `400 depth_exceeded` at the depth limit
(the MCP `delegate` tool is not even listed for such a key, and
`get_my_access` reports `can_delegate: false`); `409 name_taken`;
`409 too_many_delegations`; `429 rate_limited`; `400 invalid_name`,
`invalid_role`, `invalid_denies`, `invalid_capabilities`.

## Why it can only narrow

Nothing trusts the stored child rows. On every call the child's effective
set is recomputed (`authority/effective.py`):

- its grants' chains are walked to the root and met link by link, so a root
  grant edited narrower, or any revoked or expired link, shrinks or empties
  the child on the next call;
- the role of every key in its key chain bounds it, so lowering a parent's
  role bounds the whole subtree at once, whatever the child's own row says;
- the ceiling drops disabled or disconnected plugins;
- the merged denies are subtracted.

Two hypothesis properties check this through the agent surfaces
(`tests/test_delegation_properties.py`), after every step of random sequences
of `delegate`, `request_permission` and `revoke_delegation` (REST or MCP)
interleaved with the owner's approvals, revocations, root edits, role changes
and deny edits:

- **10.** A delegated key's effective set never exceeds its parent's; a key
  never authenticates while its parent does not; a new delegation never
  exceeds what was asked for; `revoke_delegation` succeeds exactly for a
  strict ancestor and kills the whole subtree.
- **11.** Every key's merged denies include its parent's.

The library-level versions of the same properties are in
`tests/authority/test_grant_properties.py`.

A delegated key can also ask the owner for more with `request_permission`.
The request is narrowed against its parent key's grants (never the ceiling),
so it can never be approved into more than the parent holds, and the chain
meet bounds it again at every call.

## What the owner sees

`GET /v1/admin/keys/tree` returns every key as a forest: root keys with their
delegated children nested. Each node carries the key's row (name, role, rate,
expiry, denies, `created_by`, `parent_key_id`, last use), plus:

- `status`: the key's own row, `active` | `disabled` | `expired`;
- `live`: whether it would authenticate right now, i.e. it and every ancestor
  are active and it is within the depth limit. A grandchild of a revoked key
  shows `status: active, live: false`;
- `depth`, its `grants` (with `kind`, `status`, `parent_grant_id`, and
  `decided_via: agent` for grants an agent revoked), and `children`.

A key that cannot be reached from any root (its parent row is missing, or a
corrupted parent loop) is listed at the top level with `orphan: true`; it
cannot authenticate, because its chain is broken.

Every delegation and revocation is in the audit log under the acting key's
name (`delegation.create`, `delegation.revoke`), with the child's name, role,
rate, expiry and grant ids. The plaintext key is never logged. No human
approves a delegation, so the owner's control is after the fact: disable or
edit any key in the tree, revoke any grant, or lower any role.

## Revocation propagation

Revocation writes to one key only; everything below it stops through the
chain walk, with no cascade writes:

| Event | Effect on descendants |
|---|---|
| An agent revokes a child (`revoke_delegation`) | The child key is disabled and its grants revoked (`decided_via: agent`). Every key below it gets 401 at its next call: `authenticate_bearer` walks the whole key chain. |
| The owner disables any key | The same: that key and its whole subtree get 401. |
| The owner revokes, or a grant expires | Every grant chained below it evaluates to nothing (`chain_meet` needs every link live): 403 `out_of_grant`, while the keys still authenticate. |
| The owner narrows a root grant | Every descendant shrinks to the new bound on its next call. |
| The owner lowers a key's role | Every descendant is bounded by it on its next call. |
| A plugin is disabled or disconnected | No key keeps any capability for it. |

`revoke_delegation` reaches descendants only: a key may revoke its children,
grandchildren and so on, never itself, an ancestor, a sibling, or another
lineage; all of those are a plain `404`, indistinguishable from a key id that
does not exist. An agent can only ever move a grant to `revoked`: the store
refuses `decided_via: agent` for any other transition, and for creating a
grant.

## Limits and settings

- `max_delegation_depth` (default 3, console-editable 0-10, see
  `docs/configuration.md`) is read per call through
  `auth.max_delegation_depth()` -> `runtime_settings()`, the one accessor
  shared by authentication, key creation, `delegate`, `get_my_access` and the
  MCP tool list. Raising it lets deeper keys delegate at once; lowering it
  makes every key deeper than the new limit fail authentication on its next
  request (nothing is written to their rows, so raising it again restores
  them).
- A delegation may ask for at most 87600 hours (10 years), and never beyond
  the caller's own expiry; its grants expire with the key.
- Every `delegate` attempt, granted or refused, spends one call of the
  caller's per-minute rate (the same budget as its actions): `429
  rate_limited`. A key may hold at most `max_live_delegations` live direct
  children (default 25, console-editable 1-200): `409 too_many_delegations`
  until one is revoked or expires. With the depth limit, that keeps any tree
  bounded without a human in the loop.
- An agent's `denies` must name registered targets and their declared
  resource kinds, at most 500 ids; ids are normalized by the plugin, as for
  the owner's key editor (`services/deny_input.py`).
