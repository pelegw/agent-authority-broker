"""Live effective permissions: P ∩ G(chain) ∩ R(roles) minus denies.

Covers chain revocation/expiry, root narrowed => leaf shrinks, plugin
disabled => target gone, roles along the chain, lineage checks, and denies
unioned along the key chain."""

import time

import pytest

from broker import auth, db
from broker.authority import store
from broker.authority.capability import Capability
from broker.authority.ceiling import ceiling
from broker.authority.effective import apply_denies, chain_meet, effective, is_denied, merged_denies
from broker.authority.grant import narrow
from broker.authority.roles import ROLES, role_caps

from .helpers import ECHO, GITHUB, LATTICE, MANIFESTS, WHATSAPP, all_on, bearer, tree_ancestors

NOW = lambda: int(time.time())   # noqa: E731
ROOM_CAP = Capability("echo", ["list_items", "post_item"], selector={"room": ["r1", "r2"]})


def eff(key, states=None, ancestors=tree_ancestors):
    ctx = auth.authenticate_bearer(bearer(key.plaintext))
    if ctx is None:
        return None
    return effective(ctx, NOW(), states if states is not None else all_on(ECHO, WHATSAPP),
                     MANIFESTS, ancestors)


def root_grant(principal, key, caps=(ROOM_CAP,), expires_at=None):
    return store.insert_root_grant(principal, key.key_id, list(caps), "active", "", expires_at,
                                   "owner", decided_via="session")


def delegate(principal, parent_grant, child_key, caps):
    return store.insert_child_grant(principal, child_key.key_id, "delegation",
                                    narrow(parent_grant, caps, LATTICE), "", None, None)


@pytest.fixture()
def lineage(principal, key_factory):
    """root key -> mid key -> leaf key, each with one grant in a chain."""
    k0 = key_factory(name="k0")
    k1 = key_factory(name="k1", parent=k0.key_id)
    k2 = key_factory(name="k2", parent=k1.key_id)
    g0 = root_grant(principal, k0)
    g1 = delegate(principal, g0, k1, [ROOM_CAP])
    g2 = delegate(principal, g1, k2, [Capability("echo", ["list_items"], selector={"room": ["r1"]})])
    return (k0, k1, k2), (g0, g1, g2)


# ---- ceiling and roles -------------------------------------------------------------------

def test_ceiling_only_enabled_and_connected():
    caps = ceiling("p", [(ECHO, True, True), (WHATSAPP, True, False), (GITHUB, False, True)])
    assert [c.target for c in caps] == ["echo"]
    assert caps[0].actions == ECHO.action_names and caps[0].mode == "direct"
    assert not caps[0].selector and caps[0].expires_at is None
    # only True / sqlite 1 count as on
    assert ceiling("p", [(ECHO, "1", 1), (WHATSAPP, 1, 1)])[0].target == "whatsapp"


def test_role_caps_shapes():
    reads = ECHO.actions_by_effect("read")
    writes = ECHO.actions_by_effect("write")
    destructive = ECHO.actions_by_effect("destructive")
    by = lambda role: {(c.mode, c.actions) for c in role_caps(ECHO, role)}  # noqa: E731
    assert by("read-only") == {("direct", reads)}
    assert by("read-draft") == {("direct", reads), ("draft", writes | destructive)}
    assert by("read-act") == {("direct", reads | writes), ("draft", destructive)}
    assert by("full") == {("direct", ECHO.action_names)}
    # whatsapp has no destructive actions: no empty capability is produced
    assert {c.mode for c in role_caps(WHATSAPP, "read-act")} == {"direct"}
    with pytest.raises(ValueError):
        role_caps(ECHO, "admin")
    assert ROLES == ("read-only", "read-draft", "read-act", "full")


# ---- effective ------------------------------------------------------------------------------

def test_key_without_grants_has_nothing(key_factory):
    assert eff(key_factory()) == []


def test_root_grant_bounded_by_role(principal, key_factory):
    k = key_factory(role="read-draft")
    root_grant(principal, k)
    caps = eff(k)
    modes = {c.mode: c.actions for c in caps}
    assert modes == {"direct": {"list_items"}, "draft": {"post_item"}}
    assert all(c.selector["room"] == {"r1", "r2"} for c in caps)


def test_chain_leaf_is_meet_of_chain(lineage):
    (_, _, k2), _ = lineage
    [c] = eff(k2)
    assert c.actions == {"list_items"} and c.selector["room"] == {"r1"}


def test_revoking_any_link_empties_the_leaf(lineage):
    (_, _, k2), (g0, g1, _) = lineage
    store.set_status(g1.id, "revoked", "owner", "session")
    assert eff(k2) == []


def test_expiring_any_link_empties_the_leaf(lineage):
    (_, _, k2), (g0, _, _) = lineage
    with db.connect() as conn:
        conn.execute("UPDATE grants SET expires_at = ? WHERE id = ?", (NOW() - 1, g0.id))
    assert eff(k2) == []


def test_disabling_a_parent_key_gives_401(lineage):
    (k0, _, k2), _ = lineage
    auth.disable_key(k0.key_id)
    assert eff(k2) is None


def test_root_narrowed_shrinks_the_leaf(lineage):
    (_, k1, k2), (g0, _, _) = lineage
    store.set_root_capabilities(g0.id, [Capability("echo", ["list_items"],
                                                   selector={"room": ["r2"]})])
    assert eff(k2) == []                  # r1 no longer reachable
    [c] = eff(k1)
    assert c.actions == {"list_items"} and c.selector["room"] == {"r2"}


def test_root_widened_does_not_lift_children_above_their_rows(lineage):
    (_, _, k2), (g0, _, _) = lineage
    store.set_root_capabilities(g0.id, [Capability("echo", ["*"]).replace(
        actions=ECHO.action_names)])
    [c] = eff(k2)
    assert c.actions == {"list_items"} and c.selector["room"] == {"r1"}


def test_tampered_child_row_cannot_exceed_parent(lineage):
    (_, _, k2), (_, _, g2) = lineage
    from broker.authority.capability import caps_to_json
    wide = [Capability("echo", ECHO.action_names)]
    with db.connect() as conn:
        conn.execute("UPDATE grants SET capabilities = ? WHERE id = ?", (caps_to_json(wide), g2.id))
    [c] = eff(k2)
    # bounded by the chain: actions and rooms of g1, not the tampered row
    assert c.actions <= ROOM_CAP.actions and c.selector["room"] <= {"r1", "r2"}


def test_disabled_plugin_removes_target(principal, key_factory):
    k = key_factory()
    root_grant(principal, k, caps=(ROOM_CAP, Capability("whatsapp", ["list_chats"])))
    assert {c.target for c in eff(k)} == {"echo", "whatsapp"}
    assert {c.target for c in eff(k, states=[(ECHO, False, True), (WHATSAPP, True, True)])} == {"whatsapp"}
    assert eff(k, states=[]) == []


def test_unknown_target_in_grant_is_dropped(principal, key_factory):
    k = key_factory()
    root_grant(principal, k, caps=(Capability("ghost", ["x"]), ROOM_CAP))
    assert {c.target for c in eff(k)} == {"echo"}


def test_ancestor_role_lowered_bounds_child(lineage):
    (k0, _, k2), _ = lineage
    with db.connect() as conn:
        conn.execute("UPDATE api_keys SET role = 'read-only' WHERE id = ?", (k0.key_id,))
    # k2's own role is still 'full', but its root's read-only now bounds it
    assert {c.mode for c in eff(k2)} == {"direct"}
    k0_caps = eff(k0)
    assert all(c.actions <= ECHO.actions_by_effect("read") for c in k0_caps)


def test_expired_capability_inside_grant_is_dropped(principal, key_factory):
    k = key_factory()
    root_grant(principal, k, caps=(ROOM_CAP.replace(expires_at=NOW() - 1),
                                   Capability("echo", ["watch"])))
    assert [c.actions for c in eff(k)] == [{"watch"}]


def test_corrupt_grant_fails_closed(principal, key_factory):
    k = key_factory()
    g = root_grant(principal, k)
    with db.connect() as conn:
        conn.execute("UPDATE grants SET capabilities = '{broken' WHERE id = ?", (g.id,))
    assert eff(k) == []


# ---- chain_meet structural checks ---------------------------------------------------------------

def test_chain_meet_rejects_broken_chains(lineage):
    (k0, k1, k2), (g0, g1, g2) = lineage
    ids = (k0.key_id, k1.key_id, k2.key_id)
    now = NOW()
    full = store.chain(g2.id)
    assert chain_meet(g2, full, LATTICE, now, ids)
    assert chain_meet(g2, full[1:], LATTICE, now, ids) == []         # root missing
    assert chain_meet(g2, [g0, g2], LATTICE, now, ids) == []          # link skipped
    assert chain_meet(g1, full, LATTICE, now, ids) == []              # wrong leaf
    assert chain_meet(g2, full, LATTICE, now, (k1.key_id, k2.key_id)) == []  # root key not first
    assert chain_meet(g2, full, LATTICE, now, (k2.key_id, k1.key_id, k0.key_id)) == []  # order
    assert chain_meet(g2, [], LATTICE, now) == []


def test_denies_subtract_from_explicit_selectors(principal, key_factory):
    k = key_factory(denies={"echo": {"room": ["r1"]}})
    root_grant(principal, k)
    [c] = eff(k)
    assert c.selector["room"] == {"r2"}
    k2 = key_factory(denies={"echo": {"room": ["r1", "r2"]}})
    root_grant(principal, k2)
    assert eff(k2) == []                   # every room denied -> bottom


def test_denies_union_along_chain(principal, key_factory):
    k0 = key_factory(name="k0", denies={"echo": {"room": ["r1"]}})
    k1 = key_factory(name="k1", parent=k0.key_id, denies={"echo": {"room": ["r2"]}})
    ctx0 = auth.authenticate_bearer(bearer(k0.plaintext))
    ctx1 = auth.authenticate_bearer(bearer(k1.plaintext))
    assert ctx1.denies["echo"]["room"] == ["r1", "r2"]
    assert set(ctx0.denies["echo"]["room"]) <= set(ctx1.denies["echo"]["room"])
    assert merged_denies([{"denies": '{"a": {"x": ["1"]}}'}, {"denies": {"a": {"x": ["2"]}}}]) == {
        "a": {"x": ["1", "2"]}}
    with pytest.raises(ValueError):
        merged_denies([{"denies": {"a": ["not", "a", "mapping"]}}])


def test_is_denied_and_apply_denies_helpers():
    d = {"echo": {"room": ["r1"], "folder": ["a1"]}}
    assert is_denied(d, "echo", "room", "r1") and not is_denied(d, "echo", "room", "r2")
    caps = [Capability("echo", ["list_items"], selector={"room": ["r1", "r3"]}),
            Capability("echo", ["watch"])]
    out = apply_denies(caps, d, LATTICE.forms)
    assert {c.actions: c.selector.get("room") for c in out}[frozenset({"list_items"})] == {"r3"}
    # "*" cannot be subtracted from; resolution-time is_denied covers it
    assert Capability("echo", ["watch"]) in out
