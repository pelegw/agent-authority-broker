"""Budgets: per-key per-minute limiter and per-grant capacity ledger."""

import pytest

from broker import auth, db, engine, ledger
from broker.authority import store
from broker.authority.capability import from_json, normalize_all
from broker.authority.grant import narrow
from broker.errors import PolicyError
from broker.plugins.registry import get_registry

from .conftest import cap


def post(agent, room="r1"):
    return engine.perform(agent.auth, "echo", "post_item", {"room": room, "text": "x"})


def _states():
    with db.connect() as conn:
        return [r["state"] for r in conn.execute("SELECT state FROM capacity_ledger ORDER BY id")]


def test_rate_limiter_sliding_minute():
    rl = ledger.RateLimiter()
    assert rl.check(1, 2) and rl.check(1, 2) and not rl.check(1, 2)
    assert rl.check(2, 2)          # per key


def test_per_minute_key_rate(echo_local, make_agent):
    a = make_agent([cap(["post_item", "list_items"])], rate=2)
    post(a)
    post(a)
    with pytest.raises(PolicyError) as e:
        post(a)
    assert (e.value.status, e.value.code) == (429, "rate_limited")
    assert _states() == ["committed", "committed", "released"]
    # Reads skip the ledger entirely.
    engine.perform(a.auth, "echo", "list_items", {})
    assert len(_states()) == 3


def test_per_day_budget_names_the_grant(echo_local, make_agent):
    a = make_agent([cap(["post_item"], budget={"per_day": 2})])
    post(a)
    post(a)
    with pytest.raises(PolicyError) as e:
        post(a)
    assert e.value.status == 429 and e.value.code == "budget_exhausted"
    assert a.grant_id in str(e.value)
    assert e.value.body()["grant_id"] == a.grant_id and e.value.body()["budget"] == "per_day"
    assert len(echo_local.impl.calls) == 2


def test_per_minute_budget_on_a_capability(echo_local, make_agent):
    a = make_agent([cap(["post_item"], budget={"per_minute": 1})])
    post(a)
    with pytest.raises(PolicyError) as e:
        post(a)
    assert e.value.body()["budget"] == "per_minute"


def test_parent_budget_bounds_the_delegated_chain(echo_local, make_agent, owner):
    parent = make_agent([cap(["post_item"], budget={"per_day": 2})])
    child_key = auth.create_key(owner.id, "child", "full", 60, None,
                                parent_key_id=parent.key_id, created_by="delegation")
    lattice = get_registry().lattice()
    requested = normalize_all([from_json(cap(["post_item"]))], get_registry().manifests())
    narrowed = narrow(store.get(parent.grant_id), requested, lattice)
    child_grant = store.insert_child_grant(owner.id, child_key.key_id, "delegation", narrowed,
                                           "sub-agent", None, parent.key_id)
    child = auth.authenticate_bearer(f"Bearer {child_key.plaintext}")
    post(parent)
    engine.perform(child, "echo", "post_item", {"room": "r1", "text": "x"})
    # Each call charged every grant in its chain; the root's budget is spent.
    with db.connect() as conn:
        charges = conn.execute("SELECT grant_id, COUNT(*) n FROM ledger_grants"
                               " GROUP BY grant_id").fetchall()
    assert {r["grant_id"]: r["n"] for r in charges} == {parent.grant_id: 2, child_grant.id: 1}
    with pytest.raises(PolicyError) as e:
        engine.perform(child, "echo", "post_item", {"room": "r1", "text": "x"})
    assert e.value.body()["grant_id"] == parent.grant_id


def test_503_releases_and_502_keeps_the_reservation(echo, make_agent):
    a = make_agent([cap(["post_item"], budget={"per_day": 2})])
    echo.impl.fail_next = 503
    with pytest.raises(PolicyError) as e:
        post(a)
    assert e.value.status == 503 and _states() == ["released"]
    echo.impl.fail_next = 502
    with pytest.raises(PolicyError) as e:
        post(a)
    assert e.value.status == 502 and _states() == ["released", "reserved"]
    # The unknown outcome still counts: one more call fits, then the budget is out.
    post(a)
    with pytest.raises(PolicyError):
        post(a)


def test_remaining_reports_budget_left(echo_local, make_agent):
    a = make_agent([cap(["post_item"], budget={"per_day": 3})])
    post(a)
    assert ledger.remaining([a.grant_id], "echo", "post_item") == {
        a.grant_id: {"per_day": 2}}
