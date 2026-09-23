"""Property tests for the grant algebra (plan: "Hypothesis properties").

 1. grant_le(narrow(p, req), p) always.
 2. Every narrowed cap <= some requested cap.
 3. cap_le reflexive/transitive; meet commutative, associative, idempotent,
    <= both args (and the greatest such).
 4. effective(leaf) ⊆ effective(ancestor) for every ancestor.
 5. Revoking/expiring any link => effective(leaf) = ∅; disabling a parent
    key => child 401.
 6. Narrowing a root grant => effective_after ⊆ effective_before.
 7. Disabling a plugin => no cap for it survives.
 8. JSON round-trip and idempotent normalization.
 9. Constructing NarrowedCapabilities without the sentinel raises;
    store.insert_child_grant rejects anything else.
10. (library level) Random sequences of narrow + insert_child_grant (+ approve,
    revoke, root expansions) never let a delegated key exceed its parent's
    effective set, and no grant's chain exceeds its root.
11. Denies only grow along a chain.

"⊆" between capability lists means single cover: each cap of the smaller is
<= some cap of the larger (the same relation grant_le checks).
"""

import time
import uuid

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from broker import auth, db
from broker.authority import store
from broker.authority.capability import from_json, normalize, to_json
from broker.authority.denies import denies_le
from broker.authority.effective import chain_meet, effective
from broker.authority.grant import NarrowedCapabilities, grant_le, narrow
from broker.authority.roles import ROLES

from .helpers import ALL, LATTICE, MANIFESTS, all_on, bearer, insert_principal, tree_ancestors
from .strategies import any_cap, cap_lists, denies, manifests, same_target_caps

le, mt = LATTICE.le, LATTICE.meet


def covered(smaller, larger) -> bool:
    return grant_le(list(smaller), list(larger), LATTICE)


def equiv(a, b) -> bool:
    return (a is None and b is None) or (a is not None and b is not None
                                         and le(a, b) and le(b, a))


# ---- pure algebra -------------------------------------------------------------------------

@given(parent=cap_lists(), requested=cap_lists())
def test_p1_narrow_is_le_parent(parent, requested):
    assert grant_le(narrow(parent, requested, LATTICE), parent, LATTICE)


@given(parent=cap_lists(), requested=cap_lists())
def test_p2_narrowed_le_some_requested(parent, requested):
    for c in narrow(parent, requested, LATTICE).capabilities:
        assert any(le(c, r) for r in requested)


@given(a=any_cap(live=False))
def test_p3_le_reflexive(a):
    assert le(a, a)


@given(caps=same_target_caps(3), r1=st.data())
def test_p3_le_transitive(caps, r1):
    top, x, y = caps
    b = mt(top, x)
    assume(b is not None)
    a = mt(b, y)
    assume(a is not None)
    assert le(a, b) and le(b, top) and le(a, top)
    # and the implication on unconstrained triples
    p, q, s = caps
    if le(p, q) and le(q, s):
        assert le(p, s)


@given(caps=same_target_caps(2))
def test_p3_meet_commutative_and_lower_bound(caps):
    a, b = caps
    m = mt(a, b)
    assert m == mt(b, a)
    if m is not None:
        assert le(m, a) and le(m, b)


@given(caps=same_target_caps(3))
def test_p3_meet_associative(caps):
    a, b, c = caps
    ab = mt(a, b)
    bc = mt(b, c)
    left = mt(ab, c) if ab is not None else None
    right = mt(a, bc) if bc is not None else None
    assert left == right


@given(a=any_cap(live=False))
def test_p3_meet_idempotent(a):
    m = mt(a, a)
    assert equiv(m, a)
    assert mt(m, m) == m                      # exact once canonical (subtree pruned)


@given(caps=same_target_caps(3))
def test_p3_meet_is_greatest_lower_bound(caps):
    a, b, r = caps
    x = mt(a, r)
    assume(x is not None)
    if le(x, b):
        m = mt(a, b)
        assert m is not None and le(x, m)


@given(a=any_cap(live=False), b=any_cap(live=False))
def test_p3_meet_across_targets_is_bottom(a, b):
    if a.target != b.target:
        assert mt(a, b) is None and not le(a, b)


@given(a=any_cap(live=False))
def test_p8_json_round_trip(a):
    assert from_json(to_json(a)) == a
    import json
    assert from_json(json.loads(json.dumps(to_json(a)))) == a


@given(data=st.data())
def test_p8_normalize_idempotent(data):
    from .strategies import raw_cap
    m = data.draw(manifests())
    once = normalize(data.draw(raw_cap(m)), m)
    twice = sorted({x for c in once for x in normalize(c, m)})
    assert twice == once
    for c in once:     # reads are always direct; draft caps hold writes only
        if c.mode == "draft":
            assert not (c.actions & m.actions_by_effect("read"))


@given(caps=cap_lists(), parent_id=st.none() | st.text(max_size=8), junk=st.none() | st.integers())
def test_p9_sealed_constructor(caps, parent_id, junk):
    with pytest.raises(TypeError):
        NarrowedCapabilities(tuple(caps), parent_id, LATTICE)
    with pytest.raises(TypeError):
        NarrowedCapabilities(tuple(caps), parent_id, LATTICE, junk)


@given(caps=cap_lists(), shape=st.sampled_from(["list", "tuple", "dict", "lookalike"]))
def test_p9_store_rejects_non_narrowed(caps, shape):
    class LookAlike:
        capabilities = tuple(caps)
        parent_grant_id = "g"
        lattice = LATTICE
    bogus = {"list": list(caps), "tuple": tuple(caps),
             "dict": {"capabilities": caps, "parent_grant_id": "g"},
             "lookalike": LookAlike()}[shape]
    # The type check runs before any database access.
    with pytest.raises(TypeError):
        store.insert_child_grant("p", 1, "delegation", bogus, "", None, None)


# ---- live evaluation over a real database ---------------------------------------------------

class _SharedConnection:
    """One sqlite connection reused for every db.connect() in a property test.
    Opening a connection (WAL pragma, file open) costs ~5 ms on Windows and
    examples open thousands; reusing one keeps the suite fast. close() is a
    no-op; commit/rollback/transactions behave exactly as on a fresh
    connection because every caller finishes its transaction before returning."""

    def __init__(self, conn):
        self._conn = conn

    def close(self):
        pass

    def __enter__(self):
        return self._conn.__enter__()

    def __exit__(self, *exc):
        return self._conn.__exit__(*exc)

    def __getattr__(self, name):
        return getattr(self._conn, name)


@pytest.fixture()
def fresh(env, monkeypatch):
    """Per-example reset: hypothesis reuses the function-scoped DB, so each
    example wipes keys and grants and gets a new principal."""
    real = db.connect()
    real.execute("PRAGMA synchronous=OFF")    # throwaway DB: durability irrelevant
    shared = _SharedConnection(real)
    monkeypatch.setattr(db, "connect", lambda: shared)

    def reset() -> str:
        with db.connect() as conn:
            conn.execute("DELETE FROM grants")
            conn.execute("DELETE FROM api_keys")
            conn.execute("DELETE FROM principals")
        return insert_principal()
    return reset


STATES = all_on(*ALL)


def ctx_of(key):
    return auth.authenticate_bearer(bearer(key.plaintext))


def eff(key, states=STATES):
    ctx = ctx_of(key)
    return None if ctx is None else effective(ctx, int(time.time()), states, MANIFESTS,
                                              tree_ancestors)


def new_key(principal, role, parent=None, key_denies=None):
    return auth.create_key(principal, uuid.uuid4().hex, role, 60, None, parent_key_id=parent,
                           created_by="delegation" if parent else "owner", denies=key_denies)


@st.composite
def lineage_spec(draw):
    """A root grant plus 0-3 delegation hops (chains of depth 1-4)."""
    hops = draw(st.integers(0, 3))
    return {
        "root_caps": draw(cap_lists(1, 5)),
        "roles": [draw(st.sampled_from(ROLES)) for _ in range(hops + 1)],
        "requests": [draw(cap_lists(1, 3)) for _ in range(hops)],
        "denies": [draw(denies()) for _ in range(hops + 1)],
    }


def build_lineage(principal, spec):
    """Keys root -> leaf, each holding one grant chained to the previous one.
    Roles are clamped so a child never out-ranks its parent (create_key
    would refuse). A request that narrows to bottom falls back to the
    parent's own caps so the chain always exists."""
    keys, grants = [], []
    role = spec["roles"][0]
    keys.append(new_key(principal, role, key_denies=spec["denies"][0]))
    grants.append(store.insert_root_grant(principal, keys[0].key_id, spec["root_caps"],
                                          "active", "", None, "owner", decided_via="session"))
    for i, req in enumerate(spec["requests"], start=1):
        role = ROLES[min(ROLES.index(role), ROLES.index(spec["roles"][i]))]
        keys.append(new_key(principal, role, keys[-1].key_id, spec["denies"][i]))
        n = narrow(grants[-1], req, LATTICE)
        if not n.capabilities:
            n = narrow(grants[-1], grants[-1].capabilities, LATTICE)
        grants.append(store.insert_child_grant(principal, keys[-1].key_id, "delegation", n,
                                               "", None, keys[-2].key_id))
    return keys, grants


@given(spec=lineage_spec())
def test_p4_leaf_within_every_ancestor(fresh, spec):
    keys, _ = build_lineage(fresh(), spec)
    effs = [eff(k) for k in keys]
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            assert covered(effs[j], effs[i])


@given(spec=lineage_spec(), data=st.data())
def test_p5_breaking_any_link_empties_leaf(fresh, spec, data):
    keys, grants = build_lineage(fresh(), spec)
    i = data.draw(st.integers(0, len(grants) - 1))
    how = data.draw(st.sampled_from(["revoke", "expire", "disable_key"]))
    if how == "revoke":
        assert store.set_status(grants[i].id, "revoked", "owner", "session")
        assert eff(keys[-1]) == []
    elif how == "expire":
        with db.connect() as conn:
            conn.execute("UPDATE grants SET expires_at = ? WHERE id = ?",
                         (int(time.time()) - 1, grants[i].id))
        assert eff(keys[-1]) == []
    else:
        auth.disable_key(keys[i].key_id)
        assert ctx_of(keys[-1]) is None      # 401 for the leaf and all below i


@given(spec=lineage_spec(), req=cap_lists(1, 3))
def test_p6_narrowing_root_shrinks_everyone(fresh, spec, req):
    keys, grants = build_lineage(fresh(), spec)
    before = [eff(k) for k in keys]
    n = narrow(grants[0], req, LATTICE)
    assume(n.capabilities)
    assert store.set_root_capabilities(grants[0].id, n.capabilities)
    for k, b in zip(keys, before, strict=True):
        assert covered(eff(k), b)


@given(spec=lineage_spec(), off=manifests(), how=st.sampled_from(["disabled", "disconnected"]))
def test_p7_disabled_plugin_vanishes(fresh, spec, off, how):
    keys, _ = build_lineage(fresh(), spec)
    states = [(m, not (m.id == off.id and how == "disabled"),
               not (m.id == off.id and how == "disconnected")) for m in ALL]
    for k in keys:
        assert all(c.target != off.id for c in eff(k, states))


@given(chain_denies=st.lists(denies(), min_size=1, max_size=4))
def test_p11_denies_only_grow(fresh, chain_denies):
    principal = fresh()
    keys = [new_key(principal, "full", key_denies=chain_denies[0])]
    for d in chain_denies[1:]:
        keys.append(new_key(principal, "full", keys[-1].key_id, d))
    ctxs = [ctx_of(k) for k in keys]
    for i in range(len(ctxs)):
        for j in range(i + 1, len(ctxs)):
            assert denies_le(ctxs[i].denies, ctxs[j].denies)


# ---- property 10: random operation sequences ------------------------------------------------

OPS = st.one_of(
    st.tuples(st.just("delegate"), st.integers(0, 9), cap_lists(1, 3), st.sampled_from(ROLES)),
    st.tuples(st.just("expand"), st.integers(0, 9), cap_lists(1, 3)),
    st.tuples(st.just("approve"), st.integers(0, 30)),
    st.tuples(st.just("revoke"), st.integers(0, 30)),
)


@given(root_caps=cap_lists(1, 4), root_role=st.sampled_from(ROLES),
       ops=st.lists(OPS, min_size=1, max_size=12))
def test_p10_random_sequences_never_exceed_parent(fresh, root_caps, root_role, ops):
    principal = fresh()
    root = new_key(principal, root_role)
    keys = [(root, None, root_role, 0)]            # (key, parent index, role, depth)
    grants = [store.insert_root_grant(principal, root.key_id, root_caps, "active", "", None,
                                      "owner", decided_via="session")]
    now = int(time.time())
    for op in ops:
        kind = op[0]
        if kind == "delegate":
            _, i, req, role = op
            pi = i % len(keys)
            pkey, _, prole, depth = keys[pi]
            if depth >= 3:
                continue
            role = ROLES[min(ROLES.index(role), ROLES.index(prole))]
            child = new_key(principal, role, pkey.key_id)
            keys.append((child, pi, role, depth + 1))
            for pg in store.list_active_for_key(pkey.key_id, now):
                n = narrow(pg, req, LATTICE)
                if n.capabilities:
                    grants.append(store.insert_child_grant(
                        principal, child.key_id, "delegation", n, "", None, pkey.key_id))
        elif kind == "expand":
            _, i, req = op
            key, pi, _, _ = keys[i % len(keys)]
            if pi is None:
                # A root key asks for more: bounded by the ceiling, parentless.
                from broker.authority.ceiling import ceiling
                n = narrow(ceiling(principal, STATES), req, LATTICE)
                if n.capabilities:
                    grants.append(store.insert_root_grant(
                        principal, key.key_id, n.capabilities, "pending", "", None,
                        kind="expansion"))
            else:
                for pg in store.list_active_for_key(keys[pi][0].key_id, now)[:1]:
                    n = narrow(pg, req, LATTICE)
                    if n.capabilities:
                        grants.append(store.insert_child_grant(
                            principal, key.key_id, "expansion", n, "", None, key.key_id))
        elif kind == "approve":
            store.set_status(grants[op[1] % len(grants)].id, "active", "owner", "session")
        else:
            store.set_status(grants[op[1] % len(grants)].id, "revoked", "owner", "session")

    # Invariant A: a delegated key never exceeds its parent's effective set.
    effs = [eff(k) for k, *_ in keys]
    for idx, (_, pi, _, _) in enumerate(keys):
        if pi is not None:
            assert covered(effs[idx], effs[pi])
    # Invariant B: every grant's chain meet stays within its chain's root.
    for g in grants:
        chain = store.chain(g.id)
        assert covered(chain_meet(g, chain, LATTICE, now), chain[0].capabilities)
