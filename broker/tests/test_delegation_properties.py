"""Hypothesis properties 10 and 11, driven through the agent surfaces.

tests/authority/test_grant_properties.py proves them for the library
(narrow + store). This file proves them for what an agent can actually
reach: random sequences of `delegate`, `request_permission` and
`revoke_delegation` (each over REST or the MCP tool layer, chosen per step)
interleaved with the owner's approve / revoke / root edits / deny edits,
checked with `effective` after EVERY step:

10. A delegated key's effective set never exceeds its parent's (single
    cover, the relation grant_le checks); a key never authenticates while
    its parent does not; a fresh delegation never exceeds what was asked
    for; `revoke_delegation` succeeds exactly for a strict ancestor and
    kills the whole subtree.
11. Denies only grow along a chain: every key's merged denies include its
    parent's, whatever the owner does to any single key's own set.

Operations aim where they can matter (an actor that holds something, a
pending grant, a real descendant) so most steps change state; `event()`s
record what happened (`pytest --hypothesis-show-statistics`).

Examples share one SQLite connection (as the authority suite does), because
opening one costs milliseconds on Windows and each example opens thousands.
Calls are strictly sequential (TestClient without a lifespan, so no
scheduler thread), which is what makes sharing it sound.
"""

import json
import os
import sqlite3
import time
import uuid
import zlib
from dataclasses import dataclass

import pytest
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st

from broker import auth, db, mcp_tools
from broker.authority import store
from broker.authority.capability import from_json, normalize_all, to_json
from broker.authority.denies import denies_le
from broker.authority.effective import effective
from broker.authority.grant import grant_le
from broker.authority.roles import ROLES
from broker.config import get_settings
from broker.errors import PolicyError
from broker.plugins.registry import get_registry

from .authority.helpers import ECHO
from .authority.strategies import cap_for, denies

_DEV = os.environ.get("HYPOTHESIS_PROFILE") == "dev"
# Each example is a whole scenario over HTTP, so fewer than the pure-algebra
# properties; `HYPOTHESIS_PROFILE=dev` runs many more, randomized.
PROPERTY = settings(max_examples=500 if _DEV else 100, derandomize=not _DEV, deadline=None,
                    suppress_health_check=[HealthCheck.function_scoped_fixture,
                                           HealthCheck.too_slow])


class _SharedConnection:
    """One connection reused by every db.connect() (see module docstring)."""

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
def fast_db(env, monkeypatch):
    real = sqlite3.connect(get_settings().broker_db, check_same_thread=False)
    real.row_factory = sqlite3.Row
    real.execute("PRAGMA synchronous=OFF")     # a throwaway test database
    real.execute("PRAGMA busy_timeout=10000")
    monkeypatch.setattr(db, "connect", lambda: _SharedConnection(real))
    yield
    real.close()


@pytest.fixture()
def world(fast_db, owner, echo_local, client, admin_headers):
    return client, admin_headers


# ---- strategies -------------------------------------------------------------------------

def _json_caps(caps):
    return [to_json(c) for c in caps]


ECHO_CAPS = st.lists(cap_for(ECHO), min_size=1, max_size=3).map(_json_caps)
ECHO_DENIES = denies().map(lambda d: {"echo": d["echo"]} if "echo" in d else {})
ROLE = st.none() | st.sampled_from(ROLES)
SURFACE = st.sampled_from(["rest", "mcp"])
IDX = st.integers(0, 60)

OPS = st.one_of(
    # A random request: usually clipped, which must create nothing.
    st.tuples(st.just("delegate"), IDX, ECHO_CAPS, ROLE, ECHO_DENIES, SURFACE),
    # Carved from the actor's current authority: usually granted. The deep
    # variant always extends the deepest live key, so chains reach the limit.
    st.tuples(st.just("delegate_inside"), IDX, cap_for(ECHO), ROLE, ECHO_DENIES, SURFACE),
    st.tuples(st.just("delegate_deep"), IDX, cap_for(ECHO), ROLE, ECHO_DENIES, SURFACE),
    st.tuples(st.just("request"), IDX, ECHO_CAPS, SURFACE),
    st.tuples(st.just("request_inside"), IDX, cap_for(ECHO), SURFACE),
    st.tuples(st.just("approve"), IDX),
    st.tuples(st.just("revoke_grant"), IDX),
    st.tuples(st.just("revoke_delegation"), IDX, IDX, SURFACE, st.booleans()),
    st.tuples(st.just("owner_denies"), IDX, ECHO_DENIES),
    st.tuples(st.just("owner_root"), ECHO_CAPS),
    # The owner changes any key's role (lowering an ancestor must bound its
    # whole subtree at once; raising a child must not lift it above its parent).
    st.tuples(st.just("owner_role"), IDX, st.sampled_from(ROLES), st.booleans()),
)
# Each example first grows a delegation tree from the root, so the random
# operations that follow act on real parents and children.
GROW = st.lists(st.tuples(st.sampled_from(["delegate_inside", "delegate_deep"]), IDX,
                          cap_for(ECHO), ROLE, ECHO_DENIES, SURFACE), min_size=1, max_size=4)


# ---- the world under test -----------------------------------------------------------------

@dataclass
class Key:
    key_id: int
    plaintext: str
    parent: int | None               # index into the example's key list

    @property
    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.plaintext}"}


def _effective(ctx):
    if ctx is None:
        return []
    reg = get_registry()
    return effective(ctx, int(time.time()), reg.plugin_states(), reg.manifests(), reg.ancestors)


def _ancestors(keys, i) -> list[int]:
    out = []
    while keys[i].parent is not None:
        i = keys[i].parent
        out.append(i)
    return out


def _pick(candidates: list[int], n: int) -> int | None:
    return candidates[n % len(candidates)] if candidates else None


def check(keys):
    """Properties 10 and 11 over every parent/child pair. Returns the live
    contexts and effective sets for the next step to build on."""
    lattice = get_registry().lattice()
    ctxs = [auth.context_for_key(k.key_id) for k in keys]
    effs = [_effective(c) for c in ctxs]
    for i, k in enumerate(keys):
        if k.parent is None or ctxs[i] is None:
            continue
        p = k.parent
        assert ctxs[p] is not None, "a child authenticates while its parent does not"
        assert grant_le(effs[i], effs[p], lattice), "a delegated key exceeds its parent"
        assert denies_le(ctxs[p].denies, ctxs[i].denies), "denies shrank along a chain"
    return ctxs, effs


def agent_call(client, key, ctx, surface, rest, tool, args):
    """One agent operation over REST or the MCP tool layer -> (status, body)."""
    if surface == "rest":
        method, path = rest
        r = client.request(method, path, json=args if method == "POST" and args else None,
                           headers=key.headers)
        return r.status_code, r.json()
    if ctx is None:
        return 401, {"code": "unauthorized"}     # MCP auth refuses it before any tool runs
    try:
        res = mcp_tools.dispatch(ctx, tool, args)
    except PolicyError as exc:
        return exc.status, exc.body()
    return 200, json.loads(res.content[0].text)


def _inside(lattice, caps, extra):
    """A capability <= one of `caps`: its meet with a random one, else itself.
    (crc32, not hash(): str hashing is salted per process, which would break
    the CI profile's derandomized, reproducible runs.)"""
    base = caps[zlib.crc32(json.dumps(to_json(extra), sort_keys=True).encode()) % len(caps)]
    m = lattice.meet(base, extra)
    return base if m is None else m


def _delegate(op, keys, ctxs, effs, client):
    kind, n, raw, role, deny, surface = op
    lattice = get_registry().lattice()
    if kind in ("delegate_inside", "delegate_deep"):
        holders = [x for x in range(len(keys)) if effs[x]]
        if kind == "delegate_deep" and holders:
            deepest = max(ctxs[x].depth for x in holders)
            holders = [x for x in holders if ctxs[x].depth == deepest]
        i = _pick(holders, n)
        if i is None:
            return
        req = [to_json(_inside(lattice, effs[i], raw))]
    else:
        i, req = n % len(keys), raw
    args = {"name": uuid.uuid4().hex[:12], "capabilities": req}
    if role is not None:
        args["role"] = role
    if deny:
        args["denies"] = deny
    status, body = agent_call(client, keys[i], ctxs[i], surface, ("POST", "/v1/delegations"),
                              "delegate", args)
    event(f"{kind} -> {status} " + (f"at depth {ctxs[i].depth + 1}" if status in (200, 201)
                                    else str(body.get("code"))))
    if status not in (200, 201):
        assert status in (400, 401, 409), body
        return
    child = Key(body["key_id"], body["key"], i)
    keys.append(child)
    assert auth.key_chain(child.key_id)[-2]["id"] == keys[i].key_id
    # A delegation never exceeds what was asked for.
    asked = normalize_all([from_json(c) for c in req], get_registry().enabled_manifests())
    assert grant_le(_effective(auth.context_for_key(child.key_id)), asked, lattice)


def _request(op, keys, ctxs, effs, client):
    kind, n, raw, surface = op
    lattice = get_registry().lattice()
    if kind == "request_inside":
        # A delegated key asking for something its parent holds.
        i = _pick([x for x in range(len(keys)) if keys[x].parent is not None
                   and ctxs[x] is not None and effs[keys[x].parent]], n)
        if i is None:
            return
        req = [to_json(_inside(lattice, effs[keys[i].parent], raw))]
    else:
        i, req = n % len(keys), raw
    status, body = agent_call(client, keys[i], ctxs[i], surface, ("POST", "/v1/permissions"),
                              "request_permission", {"capabilities": req})
    event(f"{kind} -> {status}")
    assert status in (200, 202, 400, 401), body


def _revoke_delegation(op, keys, ctxs, client):
    _, a, b, surface, aim = op
    if aim:     # aim at a real descendant of a live key
        i = _pick([x for x in range(len(keys)) if ctxs[x] is not None and any(
            x in _ancestors(keys, y) for y in range(len(keys)))], a)
        if i is None:
            return
        j = _pick([y for y in range(len(keys)) if i in _ancestors(keys, y)], b)
    else:
        i, j = a % len(keys), b % len(keys)
    status, body = agent_call(client, keys[i], ctxs[i], surface,
                              ("POST", f"/v1/delegations/{keys[j].key_id}/revoke"),
                              "revoke_delegation", {"key_id": keys[j].key_id})
    event(f"revoke_delegation -> {status}")
    if ctxs[i] is None:
        assert status == 401
        return
    # Descendants only: success exactly when i is a strict ancestor of j...
    assert (status == 200) == (i in _ancestors(keys, j)), (i, j, status, body)
    if status == 200:     # ...and then j's whole subtree stops authenticating.
        dead = [x for x in range(len(keys)) if x == j or j in _ancestors(keys, x)]
        assert all(auth.context_for_key(keys[x].key_id) is None for x in dead)
    else:
        assert status == 404


def _owner(op, keys, client, admin_headers):
    kind = op[0]
    if kind in ("approve", "revoke_grant"):
        grants = [g for k in keys for g in store.list_for_key(k.key_id, limit=100)]
        wanted = ("pending",) if kind == "approve" else ("pending", "active")
        g = [g for g in grants if g.status in wanted]
        if not g:
            return
        g = g[op[1] % len(g)]
        verb = "approve" if kind == "approve" else "revoke"
        r = client.post(f"/v1/admin/grants/{g.id}/{verb}", headers=admin_headers)
        event(f"{kind} -> {r.status_code}")
        assert r.status_code == 200, r.text
    elif kind == "owner_denies":
        _, i, deny = op
        r = client.patch(f"/v1/admin/keys/{keys[i % len(keys)].key_id}", json={"denies": deny},
                         headers=admin_headers)
        assert r.status_code == 200, r.text
    elif kind == "owner_root":
        r = client.patch(f"/v1/admin/keys/{keys[0].key_id}", json={"capabilities": op[1]},
                         headers=admin_headers)
        assert r.status_code == 200, r.text
    elif kind == "owner_role":
        _, n, role, aim = op      # aim: a key that has delegated (the interesting case)
        parents = sorted({k.parent for k in keys if k.parent is not None})
        i = _pick(parents, n) if aim and parents else n % len(keys)
        r = client.patch(f"/v1/admin/keys/{keys[i].key_id}", json={"role": role},
                         headers=admin_headers)
        event(f"owner_role -> {'a parent' if i in parents else 'a leaf'}")
        assert r.status_code == 200, r.text


def run_step(op, keys, ctxs, effs, client, admin_headers):
    kind = op[0]
    if kind.startswith("delegate"):
        _delegate(op, keys, ctxs, effs, client)
    elif kind.startswith("request"):
        _request(op, keys, ctxs, effs, client)
    elif kind == "revoke_delegation":
        _revoke_delegation(op, keys, ctxs, client)
    else:
        _owner(op, keys, client, admin_headers)


def _root(client, admin_headers, caps, role, deny) -> Key:
    r = client.post("/v1/admin/keys", json={
        "name": f"root-{uuid.uuid4().hex[:12]}", "role": role, "rate_per_min": 60,
        "capabilities": caps, "denies": deny}, headers=admin_headers)
    assert r.status_code == 200, r.text
    return Key(r.json()["id"], r.json()["key"], None)


# ---- the properties -------------------------------------------------------------------------

@PROPERTY
@given(root_caps=ECHO_CAPS, root_role=st.sampled_from(ROLES), root_denies=ECHO_DENIES,
       grow=GROW, ops=st.lists(OPS, min_size=2, max_size=12))
def test_p10_p11_random_sequences_never_exceed_parent(world, root_caps, root_role,
                                                      root_denies, grow, ops):
    client, admin_headers = world
    keys = [_root(client, admin_headers, root_caps, root_role, root_denies)]
    ctxs, effs = check(keys)
    for op in [*grow, *ops]:
        run_step(op, keys, ctxs, effs, client, admin_headers)
        ctxs, effs = check(keys)
    depth = max(len(_ancestors(keys, i)) for i in range(len(keys)))
    event(f"tree of {len(keys)} keys, depth {depth}")


@PROPERTY
@given(chain_denies=st.lists(ECHO_DENIES, min_size=2, max_size=4),
       edits=st.lists(st.tuples(IDX, ECHO_DENIES), max_size=4))
def test_p11_denies_only_grow_along_a_delegation_chain(world, chain_denies, edits):
    """Every hop adds its own denies; the owner then rewrites any key's own
    set (even to empty). A child's merged denies still include its parent's."""
    client, admin_headers = world
    caps = [{"target": "echo", "actions": ["list_items"]}]
    keys = [_root(client, admin_headers, caps, "full", chain_denies[0])]
    for deny in chain_denies[1:]:
        r = client.post("/v1/delegations", json={
            "name": uuid.uuid4().hex[:12], "capabilities": caps, "denies": deny},
            headers=keys[-1].headers)
        assert r.status_code == 201, r.text
        keys.append(Key(r.json()["key_id"], r.json()["key"], len(keys) - 1))
    check(keys)
    for i, deny in edits:
        _owner(("owner_denies", i, deny), keys, client, admin_headers)
        ctxs, _ = check(keys)
        assert all(c is not None for c in ctxs)
