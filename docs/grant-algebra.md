# Grant algebra

The formal spec for `broker/broker/authority/`. Every statement here has a
test; the invariants at the end are hypothesis properties in
`broker/tests/authority/test_grant_properties.py`.

## Objects

**Capability** (`capability.Capability`): an immutable allow-statement on one
target.

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

**Bottom** (⊥, nothing allowed) is `None`. A Capability with an empty action
set or an empty selector cannot be constructed.

**Grant** (`grant.Grant`): a list of capabilities held by a key, with
`kind ∈ {root, expansion, delegation}`, `status ∈ {pending, active, rejected,
expired, revoked}`, optional `expires_at`, and `parent_grant_id` (null for a
root). A grant is **live** at `now` iff `status = active ∧ (expires_at = null
∨ expires_at > now)`.

**FormTable**: `target -> {name -> FormSpec(form, values, kind)}` built from
validated manifests (`form_table`). Derived narrowings are not in it.
**Lattice** bundles a FormTable with `ancestors(kind, id) -> [ids]`, the
folder ancestry used by `subtree` (default: no ancestry, only exact ids
match).

## Order: `cap_le(child, parent)`

`child ≤ parent` iff all of:

1. same target, and the target is in the FormTable;
2. `child.actions ⊆ parent.actions`;
3. `rank(child.mode) ≤ rank(parent.mode)`;
4. `child.expires_at ≤ parent.expires_at` (null = +∞);
5. per budget field: parent absent, or child present and `≤` parent;
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
| `flag` | parent `false` ⇒ child `false` (absent = `true`) |
| `level` | `rank(child) ≤ rank(parent)` in the manifest's `values` order |

Anything the table cannot interpret (unknown target, a dimension the
manifest does not declare, a value of the wrong type or outside a level's
values, a name in the wrong slot) makes `cap_le` **false**.

## Meet: `meet(a, b)`

The greatest capability ≤ both, or ⊥:

- different targets or unknown target ⇒ ⊥;
- `actions = a ∩ b`; empty ⇒ ⊥;
- `mode = min`, `expires_at = min` (null = +∞), budget `min` per field;
- selector per dimension: absent on one side ⇒ the other side; `list`,
  `pattern` ⇒ `∩`; `subtree` ⇒ the nodes of each side that lie inside the
  other side's roots, **pruned** of any root inside another kept root (so the
  representation is canonical); an empty result ⇒ ⊥;
- constraints: `range` ⇒ `min`, `flag` ⇒ `and`, `level` ⇒ `min`; a result
  equal to top (`true`, the highest level) is dropped;
- anything uninterpretable ⇒ ⊥.

Properties: commutative, associative (exactly, thanks to pruning and
dropping tops), idempotent up to `≤`-equivalence (exactly on canonical
input), `meet(a,b) ≤ a, b`, and it is the greatest such (`x ≤ a ∧ x ≤ b ⇒
x ≤ meet(a,b)`).

## Normalization: `normalize(cap, manifest) -> [cap]`

Canonical form, applied when a capability enters the system:

1. expand action sugar (`*`, `read_*`, `write_*`, `destructive_*`);
2. validate every dimension and constraint against its declared form
   (unknown names and derived dimensions raise);
3. drop `"*"` selectors and top-valued constraints;
4. reads are always direct: a draft capability containing reads is split
   into `{reads, direct}` + `{writes, draft}`. Hence every stored draft
   capability holds writes only, and `cap_le` / `meet` can compare `mode`
   by rank without knowing side effects.

Normalization is idempotent. `[]` means ⊥.

## Grants

**Single cover**: `grant_le(child, parent)` iff every child capability is
≤ **one** parent capability. Conservative (a child capability spanning two
parent capabilities is refused), O(n·m), and each child capability has one
explainable parent.

**`narrow(parent, requested)`** = `{meet(r, p) : r ∈ requested, p ∈ parent,
same target} \ {⊥}`, deduped, wrapped in `NarrowedCapabilities`. By
construction `grant_le(narrow(p, r), p)` and every result is ≤ some requested
capability. `clipped(requested, narrowed)` lists the requested capabilities
not covered by a single narrowed one; empty means "you got exactly what you
asked for", otherwise callers return 400 with the narrowed version.

**Structural guarantee.** `NarrowedCapabilities.__post_init__` raises unless
given a sentinel private to `grant.py` (not exported), the class cannot be
subclassed, and `dataclasses.replace` cannot copy the sentinel.
`store.insert_child_grant` accepts only `type(x) is NarrowedCapabilities`,
checks the parent is live, same principal, and in the key's lineage, clamps
the child's expiry to the parent's, and re-checks `grant_le(child_row,
parent_row)` inside the same `BEGIN IMMEDIATE` transaction, rolling back on
failure. Root grants go through `insert_root_grant`, which refuses delegated
keys.

## Effective permissions (live)

```
P(owner)  = one all-"*", direct capability per plugin enabled ∧ connected
R(role)   = read-only   : reads direct
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

R is applied for **every** key in the chain, not only the caller's, so
lowering an ancestor's role bounds its descendants at once.

Nothing trusts a stored child grant: a root edited narrower shrinks every
descendant on the next call, a hand-edited child row cannot exceed its
parent, and disabling a plugin, revoking or expiring any link, or disabling
any ancestor key (authentication walks the key chain) takes effect with no
cascade writes. Any unparseable grant row makes `effective` return `[]` for
that key.

## Denies (outside the lattice)

The lattice has no deny. Denies are sets `{target: {kind: [ids]}}`:

- per key (`api_keys.denies`); a key's effective denies are the **union**
  along its key chain (`merged_denies`), computed at authentication;
- owner-level `hidden_resources` (phase 3).

`apply_denies` subtracts denied ids from explicit selectors whose dimension's
resource kind matches (an emptied selector makes the capability ⊥).
`"*"` selectors cannot be subtracted from; `is_denied` is the
resolution-time check the policy engine applies to the concrete resource.

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

CI runs 200 derandomized examples per property; `HYPOTHESIS_PROFILE=dev`
runs 1000 random ones.

## Known conservative choices

- Single cover refuses a child capability that two parent capabilities
  could jointly cover.
- `pattern` is compared as exact strings; `feat/*` does not cover `feat/x`.
- A selector dimension that does not apply to some of a capability's actions
  is still compared; a request carrying an irrelevant restriction may be
  refused as not ≤ rather than accepted. Never permissive.
