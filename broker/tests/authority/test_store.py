"""The grants store: only narrow() output can create a child grant, the
post-condition catches a tampered parent row, and statuses move only along
allowed transitions."""

import dataclasses
import time

import pytest

from broker import db
from broker.authority import grant as grant_mod
from broker.authority import store
from broker.authority.capability import Capability, caps_to_json
from broker.authority.grant import NarrowedCapabilities, clipped, narrow

from .helpers import LATTICE

ECHO_ALL = Capability("echo", ["list_items", "get_item", "post_item"],
                      selector={"room": ["r1", "r2"]})


def root(principal, key_id, caps=(ECHO_ALL,), status="active", expires_at=None):
    return store.insert_root_grant(principal, key_id, list(caps), status, "owner grant",
                                   expires_at, decided_by="owner", decided_via="session")


@pytest.fixture()
def family(principal, key_factory):
    parent = key_factory(name="parent")
    child = key_factory(name="child", parent=parent.key_id)
    g = root(principal, parent.key_id)
    return parent, child, g


def test_root_grant_round_trip(principal, key_factory):
    k = key_factory()
    g = root(principal, k.key_id)
    got = store.get(g.id)
    assert got == g and got.status == "active" and got.decided_by_principal == "owner"
    assert store.list_for_key(k.key_id) == [g]
    assert store.chain(g.id) == [g]


def test_root_grant_validation(principal, key_factory):
    k = key_factory()
    with pytest.raises(ValueError):
        store.insert_root_grant(principal, k.key_id, [], "active", "", None)
    with pytest.raises(TypeError):
        store.insert_root_grant(principal, k.key_id, [{"target": "echo"}], "active", "", None)
    with pytest.raises(ValueError):
        root(principal, k.key_id, status="revoked")
    with pytest.raises(ValueError):
        root("other-principal", k.key_id)
    with pytest.raises(ValueError):
        store.insert_root_grant(principal, k.key_id, [ECHO_ALL], "active", "", None,
                                kind="delegation")
    with pytest.raises(ValueError):
        store.insert_root_grant(principal, k.key_id, [ECHO_ALL], "active", "", None,
                                decided_via="carrier-pigeon")


def test_delegated_key_cannot_get_a_root_grant(family, principal):
    _, child, _ = family
    with pytest.raises(ValueError, match="child grants"):
        root(principal, child.key_id)


def test_child_grant_from_narrow(family, principal):
    parent, child, g = family
    req = [Capability("echo", ["list_items"], selector={"room": ["r1"]})]
    n = narrow(g, req, LATTICE)
    c = store.insert_child_grant(principal, child.key_id, "delegation", n, "sub-agent",
                                 None, parent.key_id)
    assert c.parent_grant_id == g.id and c.status == "active" and c.kind == "delegation"
    assert store.chain(c.id) == [g, c]
    e = store.insert_child_grant(principal, child.key_id, "expansion", n, "please", None,
                                 child.key_id)
    assert e.status == "pending"        # expansions wait for a human


def test_hand_built_narrowed_capabilities_raise(family):
    _, _, g = family
    with pytest.raises(TypeError):
        NarrowedCapabilities((ECHO_ALL,), g.id, LATTICE)
    with pytest.raises(TypeError):
        NarrowedCapabilities((ECHO_ALL,), g.id, LATTICE, object())
    legit = narrow(g, [ECHO_ALL], LATTICE)
    with pytest.raises(TypeError, match="narrow"):
        dataclasses.replace(legit, capabilities=(Capability("echo", ["delete_item"]),))
    with pytest.raises(TypeError):
        class Sneaky(NarrowedCapabilities):   # noqa: F841
            pass
    assert "_PROOF" not in grant_mod.__all__


def test_store_rejects_anything_but_narrowed(family, principal):
    parent, child, g = family

    class LookAlike:
        capabilities = (ECHO_ALL,)
        parent_grant_id = g.id
        lattice = LATTICE

    for bogus in ([ECHO_ALL], (ECHO_ALL,), {"capabilities": [ECHO_ALL]}, LookAlike(), None):
        with pytest.raises(TypeError):
            store.insert_child_grant(principal, child.key_id, "delegation", bogus, "", None,
                                     parent.key_id)
    assert store.list_for_key(child.key_id) == []


def test_child_insert_rules(family, principal, key_factory):
    parent, child, g = family
    n = narrow(g, [ECHO_ALL], LATTICE)
    with pytest.raises(ValueError):          # wrong kind
        store.insert_child_grant(principal, child.key_id, "root", n, "", None, None)
    with pytest.raises(ValueError):          # narrowed against the ceiling, no parent row
        store.insert_child_grant(principal, child.key_id, "delegation",
                                 narrow([ECHO_ALL], [ECHO_ALL], LATTICE), "", None, None)
    with pytest.raises(ValueError):          # bottom: nothing survived
        store.insert_child_grant(principal, child.key_id, "delegation",
                                 narrow(g, [Capability("echo", ["watch"])], LATTICE),
                                 "", None, None)
    stranger = key_factory(name="stranger")  # not in the parent's lineage
    with pytest.raises(ValueError, match="lineage"):
        store.insert_child_grant(principal, stranger.key_id, "delegation", n, "", None, None)
    store.set_status(g.id, "revoked", "owner", "session")
    with pytest.raises(ValueError, match="not active"):
        store.insert_child_grant(principal, child.key_id, "delegation", n, "", None, None)


def test_child_expiry_clamped_to_parent(principal, key_factory):
    p = key_factory(name="p")
    c = key_factory(name="c", parent=p.key_id)
    exp = int(time.time()) + 100
    g = root(principal, p.key_id, expires_at=exp)
    n = narrow(g, [ECHO_ALL], LATTICE)
    assert store.insert_child_grant(principal, c.key_id, "delegation", n, "", None,
                                    None).expires_at == exp
    assert store.insert_child_grant(principal, c.key_id, "delegation", n, "", exp + 50,
                                    None).expires_at == exp


def test_post_condition_fires_on_tampered_parent_row(family, principal):
    parent, child, g = family
    n = narrow(g, [ECHO_ALL], LATTICE)
    # The parent row is narrowed underneath us after narrow() ran.
    shrunk = [Capability("echo", ["list_items"], selector={"room": ["r1"]})]
    with db.connect() as conn:
        conn.execute("UPDATE grants SET capabilities = ? WHERE id = ?",
                     (caps_to_json(shrunk), g.id))
    with pytest.raises(store.GrantInvariantError):
        store.insert_child_grant(principal, child.key_id, "delegation", n, "", None,
                                 parent.key_id)
    assert store.list_for_key(child.key_id) == []     # rolled back


def test_clipped_reports_what_was_lost(family):
    _, _, g = family
    fits = Capability("echo", ["list_items"], selector={"room": ["r1"]})
    too_big = Capability("echo", ["list_items", "delete_item"])
    n = narrow(g, [fits, too_big], LATTICE)
    assert clipped([fits, too_big], n) == [too_big]
    assert clipped([fits], narrow(g, [fits], LATTICE)) == []


def test_status_transitions(family, principal):
    parent, child, g = family
    pend = store.insert_child_grant(principal, child.key_id, "expansion",
                                    narrow(g, [ECHO_ALL], LATTICE), "", None, child.key_id)
    assert store.set_status(pend.id, "active", "owner", "telegram")
    got = store.get(pend.id)
    assert got.status == "active" and got.decided_via == "telegram"
    assert not store.set_status(pend.id, "rejected")        # only pending -> rejected
    assert store.set_status(pend.id, "revoked", "owner", "token")
    assert not store.set_status(pend.id, "active")           # terminal
    with pytest.raises(ValueError):
        store.set_status(pend.id, "pending")
    with pytest.raises(ValueError):
        store.set_status(pend.id, "revoked", "owner", "email")
    assert not store.set_status("missing", "revoked")


def test_live_predicate_and_sweep(principal, key_factory):
    k = key_factory()
    now = int(time.time())
    live = root(principal, k.key_id)
    stale = root(principal, k.key_id, expires_at=now - 1)
    pending = root(principal, k.key_id, status="pending")
    assert [g.id for g in store.list_active_for_key(k.key_id, now)] == [live.id]
    assert {g.id for g in store.list_for_key(k.key_id)} == {live.id, stale.id, pending.id}
    assert store.sweep_expired(now) == 1
    assert store.get(stale.id).status == "expired"


def test_set_root_capabilities_only_on_roots(family, principal):
    parent, child, g = family
    c = store.insert_child_grant(principal, child.key_id, "delegation",
                                 narrow(g, [ECHO_ALL], LATTICE), "", None, None)
    new = [Capability("echo", ["list_items"])]
    assert store.set_root_capabilities(g.id, new)
    assert list(store.get(g.id).capabilities) == new
    assert not store.set_root_capabilities(c.id, new)


def test_corrupt_capabilities_row_raises_on_read(principal, key_factory):
    k = key_factory()
    g = root(principal, k.key_id)
    with db.connect() as conn:
        conn.execute("UPDATE grants SET capabilities = ? WHERE id = ?",
                     ('[{"target":"echo","actions":["x"],"sneaky":1}]', g.id))
    with pytest.raises(ValueError):
        store.get(g.id)
