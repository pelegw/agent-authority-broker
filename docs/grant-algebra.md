# Grant algebra

This is the formal spec for `broker/broker/authority/`. Every statement here
has a test. The invariants at the end are hypothesis properties in
`broker/tests/authority/test_grant_properties.py`.

## Objects

**Capability** (`capability.Capability`): an immutable statement that permits
actions on one target.

```json
{"target": "echo",
 "actions": ["list_items", "post_item"],
 "selector": {"room": ["r1", "r2"], "folder": ["a"]},
 "constraints": {"window_days": 30, "attachments": false, "visibility": "summary"},
 "mode": "draft",
 "expires_at": 1760000000,
 "budget": {"per_day": 40}}
```

- `actions`: explicit set of manifest action names.
- `selector`: set-valued dimensions (forms `list`, `subtree`, `pattern`),
  value = non-empty id set. Absent dimension = `"*"` = unrestricted.
- `constraints`: scalar dimensions (forms `range`, `flag`, `level`),
  whether the manifest declares them as narrowings or constraints. Absent =
  unrestricted.
- `mode`: `draft < direct`.
- `expires_at`: unix seconds; `null` = never (+∞).
- `budget`: `{per_minute?, per_day?}`; absent field = unlimited.

**Bottom** (⊥, nothing permitted) is `None`. The constructor rejects a
Capability with an empty action set or an empty selector.

**Grant** (`grant.Grant`): a list of capabilities that a key holds, with these
fields:

- `kind ∈ {root, expansion, delegation}`.
- `status ∈ {pending, active, rejected, expired, revoked}`.
- Optional `expires_at`.
- `parent_grant_id` (null for a root).

A grant is **live** at `now` iff
`status = active ∧ (expires_at = null ∨ expires_at > now)`.

**FormTable**: `target -> {name -> FormSpec(form, values, kind)}`.
`form_table` builds it from validated manifests. Narrowings with
`derived_from` are not in it.
**Lattice** bundles a FormTable with `ancestors(kind, id) -> [ids]`. That is
the folder ancestry that `subtree` uses. The default has no ancestry: only
exact ids match.

## Order: `cap_le(child, parent)`

`child ≤ parent` iff all of:

1. same target, and the target is in the FormTable.
2. `child.actions ⊆ parent.actions`.
3. `rank(child.mode) ≤ rank(parent.mode)`.
4. `child.expires_at ≤ parent.expires_at` (null = +∞).
5. per budget field: parent absent, or child present and `≤` parent.
6. per selector dimension (union of both sides), by form:

| form | `≤` |
|---|---|
| `list` | parent absent (`"*"`) accepts all; else child present and child ⊆ parent |
| `subtree` | parent absent accepts all; else every child root is inside some parent root (`x` inside `R` iff `x ∈ R` or some `ancestors(kind, x)` ∈ `R`) |
| `pattern` | as `list`: exact-string subset. No glob implication (`a@x.com` is not ≤ `*@x.com`), which keeps it decidable |

7. per constraint (union of both sides), with absent = top:

| form | `≤` |
|---|---|
| `range` | parent absent, or child present and `child ≤ parent` |
| `flag` | parent `false` ⇒ child `false` (absent = `true`). `true` is top, so a flag must be named for the permission it grants (`attachments`, `file_content`, not `metadata_only`); see below |
| `level` | `rank(child) ≤ rank(parent)` in the manifest's `values` order |

`cap_le` is **false** for anything that the table cannot interpret:

- An unknown target.
- A dimension that the manifest does not declare.
- A value of the wrong type, or outside the values of a level.
- A name in the wrong slot.

## Meet: `meet(a, b)`

The result is the greatest capability ≤ both, or ⊥:

- different targets or unknown target ⇒ ⊥.
- `actions = a ∩ b`; empty ⇒ ⊥.
- `mode = min`, `expires_at = min` (null = +∞), budget `min` per field.
- selector per dimension:
  - absent on one side ⇒ the other side.
  - `list`, `pattern` ⇒ `∩`.
  - `subtree` ⇒ the nodes of each side that lie inside the other side's
    roots, **pruned** of any root inside another kept root (so the
    representation is canonical).
  - an empty result ⇒ ⊥.
- constraints: `range` ⇒ `min`, `flag` ⇒ `and`, `level` ⇒ `min`. The meet
  drops a result equal to top (`true`, the highest level).
- anything uninterpretable ⇒ ⊥.

`meet` has these properties:

- It is commutative.
- It is associative (exactly, thanks to pruning and dropping tops).
- It is idempotent up to `≤`-equivalence (exactly on canonical input).
- `meet(a,b) ≤ a, b`.
- It is the greatest such (`x ≤ a ∧ x ≤ b ⇒ x ≤ meet(a,b)`).

## Normalization: `normalize(cap, manifest) -> [cap]`

`normalize` makes the canonical form when a capability enters the system:

1. expand action sugar (`*`, `read_*`, `write_*`, `destructive_*`).
2. validate every dimension and constraint against its declared form
   (unknown names and `derived_from` dimensions raise).
3. drop `"*"` selectors and top-valued constraints.
4. reads are always direct: split a draft capability that contains reads
   into `{reads, direct}` + `{writes, draft}`. Hence every stored draft
   capability holds writes only. Thus `cap_le` / `meet` can compare `mode`
   by rank without knowing side effects.

Normalization is idempotent. `[]` means ⊥.

## Grants

**Single cover**: `grant_le(child, parent)` iff every child capability is
≤ **one** parent capability. This rule is conservative: it rejects a child
capability that spans two parent capabilities. It costs O(n·m). Each child
capability has one explainable parent.

**`narrow(parent, requested)`** = `{meet(r, p) : r ∈ requested, p ∈ parent,
same target} \ {⊥}`, deduped, wrapped in `NarrowedCapabilities`. By
construction `grant_le(narrow(p, r), p)` and every result is ≤ some requested
capability. `clipped(requested, narrowed)` lists the requested capabilities
that no single narrowed capability covers. Empty means "you got exactly what
you asked for". Otherwise, callers return 400 with the narrowed version.

**Structural protection.** `NarrowedCapabilities.__post_init__` raises unless
it gets a sentinel that is private to `grant.py` and not exported. The class
does not permit subclasses. `dataclasses.replace` cannot copy the sentinel.
`store.insert_child_grant` does these steps:

- It accepts only `type(x) is NarrowedCapabilities`.
- It checks that the parent is live, has the same principal, and is in the
  lineage of the key.
- It clamps the expiry of the child to the expiry of the parent.
- It checks `grant_le(child_row, parent_row)` again inside the same
  `BEGIN IMMEDIATE` transaction, and rolls back on failure.

Root grants go through `insert_root_grant`, which rejects delegated keys.

## Effective permissions (live)

```
P(owner)  = one all-"*", direct capability per plugin enabled ∧ connected
R(role)   = the key's ceiling, all-"*" capabilities shaped by side effect:
            read-only   : reads direct
            read-draft  : reads direct; writes + destructive draft
            read-act    : reads + writes direct; destructive draft
            full        : everything direct
chain_meet(g) = fold meet down the chain root -> ... -> g
                (⊥ unless every link is live, the chain is intact, one
                 principal, root kind root|expansion, other links
                 expansion|delegation, and the grants' keys walk the key
                 lineage root key first, never upward)
G(key)    = ⋃ { chain_meet(g) : g a live grant of the key }
effective = { meet(meet(c, p), r1, ..., rn) : c ∈ G, p ∈ P,
              r_i ∈ R(role of the i-th key in the key chain) } \ {⊥}
            minus the key's merged denies, minus expired capabilities
```

The broker applies R for **every** key in the chain, not only for the key of
the caller. Thus lowering the role of an ancestor bounds its descendants at
once.

**The role is a ceiling.** It never grants anything. A key with no grants has
an empty G, and a meet with R cannot add to it. It only caps every capability
below it, per side effect:

- Under `read-draft`, the writes of a direct capability run as drafts.
- Under `read-only`, the broker denies those writes.
- Under `full` (everything direct), nothing changes, so the capabilities
  decide.

That is why a key that the owner creates defaults to `full`
(`auth.OWNER_KEY_DEFAULT_ROLE`). A lower ceiling is a choice of the owner. A
delegated key defaults to the role of its caller and never exceeds it. The
mode of each role per side effect only rises with its rank. Thus meeting the
roles of a whole key chain is exactly meeting the lowest one. `get_my_access`
reports that role as the `ceiling` of the key (`role_ceiling.key_ceiling`).

`broker/broker/role_ceiling.py` spells out what the ceiling does to each
action, for three readers:

- The agent: `get_my_access` shows `effective_mode` wherever the ceiling
  lowers an action, as `draft` or `denied`.
- The owner: the `ceiling_note` of the pending-grant list, and the Telegram
  card. If the ceiling caps a request, approving it does not do what the
  agent asked.
- The capability editor of the console: the same rule on the client side,
  from a table that a test holds equal to `roles.role_caps`.

The module uses the same pieces as the engine: `roles.role_caps`, the lower
mode of the meet, and `policy.run_mode`. It only describes. `policy.evaluate`
decides. A test holds the description to real engine decisions for every
ceiling, mode and side effect.

Nothing trusts a stored child grant:

- A root that becomes narrower shrinks every descendant on the next call.
- A hand-edited child row cannot exceed its parent.
- These changes take effect with no cascade writes: disabling a plugin,
  revoking or expiring any link, or disabling any ancestor key.
  Authentication walks the key chain.

Any unparseable grant row makes `effective` return `[]` for that key.

## Denies (outside the lattice)

The lattice has no deny. Denies are sets `{target: {kind: [ids]}}`:

- Per key (`api_keys.denies`). The effective denies of a key are the
  **union** along its key chain (`merged_denies`). The broker calculates them
  at authentication.
- Owner-level `hidden_resources` (phase 3).

`apply_denies` subtracts denied ids from the explicit selectors whose
dimension has a matching resource kind. An emptied selector makes the
capability ⊥. `apply_denies` cannot subtract from `"*"` selectors.
`is_denied` is the resolution-time check that the policy engine applies to
the concrete resource.

## Invariants (hypothesis properties)

1. `grant_le(narrow(p, req), p)`.
2. Every narrowed capability ≤ some requested capability.
3. `cap_le` reflexive and transitive; `meet` commutative, associative,
   idempotent, a lower bound, and the greatest lower bound.
4. `effective(leaf) ⊆ effective(ancestor)` for every ancestor (single cover).
5. Revoking or expiring any link ⇒ `effective(leaf) = ∅`; disabling an
   ancestor key ⇒ the leaf does not authenticate.
6. Narrowing a root grant ⇒ `effective_after ⊆ effective_before` for every
   key in the lineage.
7. Disabling or disconnecting a plugin ⇒ no capability for it survives.
8. JSON round trip; normalization idempotent; draft capabilities hold no reads.
9. `NarrowedCapabilities` cannot be built without the sentinel;
   `insert_child_grant` rejects everything else.
10. Random sequences of delegate / expand / approve / revoke never let a
    delegated key exceed its parent's effective set, and no grant's chain
    meet exceeds its root.
11. Denies only grow along a key chain.

CI runs 200 derandomized examples per property. `HYPOTHESIS_PROFILE=dev`
runs 1000 random ones.

## Flag polarity (a rule for manifest authors)

`true` is top for a `flag`:

- An absent flag means `true`.
- Normalization drops `true`.
- `meet` drops a result equal to `true`.

Thus a flag can only ever restrict by being `false`. Its name must state
**the permission it grants**. Consider a flag named for a restriction,
for example `hide_private: true` or `metadata_only: true`. Normalization
drops it as top, and it restricts nothing. The grant then fails open without
any error. Thus the Google manifests use `private_events`, `others_events`
and `file_content`, and restrict with `false`. The plan said `hide_private`,
`own_events_only` and `metadata_only`. The tests of the Google manifests lint
the flag names. Likewise, the last value of a `level` is top. Thus its values
run from the most restrictive to the most permissive (`[freebusy, full]`,
`[draft, direct]`).

## Known conservative choices

- Single cover rejects a child capability that no single parent capability
  covers, even when two parent capabilities cover it together.
- The broker compares `pattern` as exact strings. `feat/*` does not cover
  `feat/x`.
- The broker still compares a selector dimension that does not apply to some
  actions of a capability. Thus it can reject a request that carries an
  irrelevant restriction as not ≤, and not accept it. Never permissive.
