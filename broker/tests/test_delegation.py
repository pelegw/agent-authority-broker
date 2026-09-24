"""Delegation: an agent mints a narrower child key (REST + MCP).

Unit cases here; the hypothesis properties 10 and 11 (random sequences of
delegate / request_permission / approve / revoke never let a delegated key
exceed its parent, denies only grow along a chain) are in
tests/test_delegation_properties.py.
"""

import time

import pytest

from broker import auth, db
from broker.authority import store
from broker.plugins import settings

from .conftest import cap
from .mcp_helpers import call, live, text_json, tools  # noqa: F401

LIST = "/v1/targets/echo/actions/list_items"
POST = "/v1/targets/echo/actions/post_item"


def delegate(client, agent_headers, caps, name="helper", **kw):
    return client.post("/v1/delegations", json={"name": name, "capabilities": caps, **kw},
                       headers=agent_headers)


def bearer(body) -> dict:
    return {"Authorization": f"Bearer {body['key']}"}


def list_room(client, headers, room):
    return client.post(LIST, json={"params": {"room": room}}, headers=headers)


def post_room(client, headers, room, **kw):
    return client.post(POST, json={"params": {"room": room, "text": "x"}, **kw},
                       headers=headers)


@pytest.fixture()
def parent(echo_local, make_agent):
    """A root key reading and posting in rooms r1, r2."""
    return make_agent([cap(["list_items", "post_item"], selector={"room": ["r1", "r2"]})],
                      name="bot")


def chain(client, parent, depth):
    """parent -> d1 -> ... -> d<depth>, each holding list_items on r1.
    Returns the header dicts, parent first."""
    out = [parent.headers]
    for i in range(depth):
        r = delegate(client, out[-1], [cap(["list_items"], selector={"room": ["r1"]})],
                     name=f"d{i + 1}")
        assert r.status_code == 201, r.text
        out.append(bearer(r.json()))
    return out


# ---- delegate --------------------------------------------------------------------------

def test_delegate_mints_a_narrower_child(client, parent):
    r = delegate(client, parent.headers, [cap(["list_items"], selector={"room": ["r1"]})],
                 reason="summarize r1")
    assert r.status_code == 201, r.text
    assert r.headers["cache-control"] == "no-store"         # it carries a secret
    body = r.json()
    assert body["name"] == "bot/helper" and body["key"].startswith("aab_")
    assert body["expires_at"] is None and body["role"] == "full"
    assert body["capabilities"] == [{
        "target": "echo", "actions": ["list_items"], "selector": {"room": ["r1"]},
        "constraints": {}, "mode": "direct", "expires_at": None, "budget": {}}]
    child = bearer(body)
    assert list_room(client, child, "r1").status_code == 200
    assert list_room(client, child, "r2").status_code == 403      # narrower than the parent
    assert post_room(client, child, "r1").status_code == 403      # action not delegated
    me = client.get("/v1/me", headers=child).json()
    assert (me["depth"], me["delegated"], me["parent"]) == (1, True, "bot")
    assert client.get("/v1/me", headers=parent.headers).json()["delegations"] == 1
    grants = store.list_for_key(body["key_id"])
    assert [(g.kind, g.status, g.parent_grant_id, g.requested_by_key_id) for g in grants] == [
        ("delegation", "active", parent.grant_id, parent.key_id)]
    assert grants[0].reason == "summarize r1"


def test_delegation_is_audited_under_the_caller_and_never_logs_the_key(client, parent):
    body = delegate(client, parent.headers, [cap(["list_items"], selector={"room": ["r1"]})]
                    ).json()
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM audit_log")]
    [row] = [r for r in rows if r["action"] == "delegation.create"]
    assert row["actor"] == "bot" and row["resource"] == str(body["key_id"])
    assert all(body["key"] not in str(r) for r in rows)


def test_clipped_request_is_400_with_the_clipped_list_and_nothing_created(client, parent):
    wide = cap(["list_items"], selector={"room": ["r1", "r3"]})
    r = delegate(client, parent.headers, [wide])
    assert r.status_code == 400
    body = r.json()
    assert body["code"] == "clipped"
    assert body["clipped"] == [{
        "target": "echo", "actions": ["list_items"], "selector": {"room": ["r1", "r3"]},
        "constraints": {}, "mode": "direct", "expires_at": None, "budget": {}}]
    assert [c["selector"] for c in body["allowed"]] == [{"room": ["r1"]}]
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM api_keys").fetchone()[0] == 1


def test_an_unrestricted_request_cannot_come_out_of_a_restricted_parent(client, parent):
    r = delegate(client, parent.headers, [cap(["list_items"])])      # any room
    assert r.status_code == 400 and r.json()["code"] == "clipped"


def test_allowed_list_never_shows_a_hidden_resource(client, echo_local, make_agent):
    from broker import hidden
    p = make_agent([cap(["list_items"], selector={"folder": ["a1", "a2"]})])
    # Asking for the whole of folder "a" clips to the parent's a1 and a2...
    r = delegate(client, p.headers, [cap(["list_items"], selector={"folder": ["a"]})])
    assert r.status_code == 400
    assert [c["selector"] for c in r.json()["allowed"]] == [{"folder": ["a1", "a2"]}]
    # ...and once the owner hides a2, the refusal no longer mentions it.
    hidden.add("echo", "folder", "a2")
    r = delegate(client, p.headers, [cap(["list_items"], selector={"folder": ["a"]})])
    assert r.status_code == 400 and "a2" not in r.text
    assert [c["selector"] for c in r.json()["allowed"]] == [{"folder": ["a1"]}]


def test_ids_under_a_hidden_folder_are_never_shown(client, echo_local, make_agent):
    from broker import hidden
    p = make_agent([cap(["list_items"], selector={"folder": ["a1", "b1"]})])
    hidden.add("echo", "folder", "a")            # hides a's whole subtree, a1 included
    r = delegate(client, p.headers, [cap(["list_items"], selector={"folder": ["root"]})])
    assert r.status_code == 400 and "a1" not in r.text
    assert [c["selector"] for c in r.json()["allowed"]] == [{"folder": ["b1"]}]
    # The same rule in get_my_access, which shares the filter.
    me = client.get("/v1/me", headers=p.headers).json()
    assert [c["selector"] for c in me["targets"]["echo"]["capabilities"]] == [
        {"folder": ["b1"]}]


def test_delegating_spends_the_callers_rate(client, echo_local, make_agent):
    p = make_agent([cap(["list_items"], selector={"room": ["r1"]})], rate=2)
    c = [cap(["list_items"], selector={"room": ["r1"]})]
    assert delegate(client, p.headers, c, name="one").status_code == 201
    assert delegate(client, p.headers, [cap(["post_item"])], name="two").status_code == 400
    r = delegate(client, p.headers, c, name="three")       # refused attempts count too
    assert r.status_code == 429 and r.json()["code"] == "rate_limited"


def test_live_children_are_capped(client, parent, monkeypatch):
    from broker.services import delegation
    monkeypatch.setattr(delegation, "MAX_LIVE_CHILDREN", 2)
    c = [cap(["list_items"], selector={"room": ["r1"]})]
    first = delegate(client, parent.headers, c, name="a").json()
    assert delegate(client, parent.headers, c, name="b").status_code == 201
    r = delegate(client, parent.headers, c, name="c")
    assert r.status_code == 409 and r.json()["code"] == "too_many_delegations"
    client.post(f"/v1/delegations/{first['key_id']}/revoke", headers=parent.headers)
    assert delegate(client, parent.headers, c, name="c").status_code == 201


def test_draft_parent_can_only_delegate_draft(client, echo_local, make_agent):
    p = make_agent([cap(["post_item"], selector={"room": ["r1"]}, mode="draft")])
    direct = cap(["post_item"], selector={"room": ["r1"]})
    assert delegate(client, p.headers, [direct]).json()["code"] == "clipped"
    r = delegate(client, p.headers, [{**direct, "mode": "draft"}])
    assert r.status_code == 201
    queued = post_room(client, bearer(r.json()), "r1")
    assert queued.status_code == 202 and queued.json()["status"] == "pending_approval"


def test_the_childs_role_bounds_what_it_can_be_given(client, parent):
    # A read-only child cannot be handed a write: that would be a capability it
    # could never use, so the request is refused rather than silently shrunk.
    post = cap(["post_item"], selector={"room": ["r1"]})
    r = delegate(client, parent.headers, [post], role="read-only")
    assert r.status_code == 400 and r.json()["code"] == "clipped"
    r = delegate(client, parent.headers, [cap(["list_items"], selector={"room": ["r1"]})],
                 role="read-only")
    assert r.status_code == 201 and r.json()["role"] == "read-only"


def test_role_rate_and_lifetime_are_at_most_the_parents(client, echo_local, make_agent):
    soon = int(time.time()) + 3600
    p = make_agent([cap(["list_items"], selector={"room": ["r1"]})], role="read-act", rate=10,
                   expires_at=soon)
    c = [cap(["list_items"], selector={"room": ["r1"]})]
    for kw, field in (({"role": "full"}, "role"), ({"rate_per_min": 11}, "rate_per_min"),
                      ({"expires_in_hours": 2}, "expires_in_hours")):
        r = delegate(client, p.headers, c, name=f"x{field[:4]}", **kw)
        assert r.status_code == 400, (kw, r.text)
        assert r.json()["code"] == "exceeds_parent" and r.json()["field"] == field
    # Defaults inherit the parent's role, rate and expiry.
    body = delegate(client, p.headers, c).json()
    row = auth.key_chain(body["key_id"])[-1]
    assert (row["role"], row["rate_per_min"], row["expires_at"]) == ("read-act", 10, soon)
    assert body["expires_at"] == soon
    # A shorter lifetime is fine, and the grant dies with the key.
    body = delegate(client, p.headers, c, name="short", expires_in_hours=1,
                    rate_per_min=3, role="read-only").json()
    assert body["expires_at"] <= soon
    assert store.list_for_key(body["key_id"])[0].expires_at == body["expires_at"]


def test_child_cannot_outlive_parent_even_when_it_asks_for_nothing(client, echo_local,
                                                                  make_agent):
    soon = int(time.time()) + 3600
    p = make_agent([cap(["list_items"], selector={"room": ["r1"]})], expires_at=soon)
    body = delegate(client, p.headers, [cap(["list_items"], selector={"room": ["r1"]})]).json()
    assert body["expires_at"] == soon
    ctx = auth.context_for_key(body["key_id"])
    assert ctx.expires_at == soon


def test_depth_cap(client, live, parent):
    headers = chain(client, parent, 3)                   # max_delegation_depth = 3
    deepest, above = headers[-1], headers[-2]
    me = client.get("/v1/me", headers=deepest).json()
    assert me["depth"] == 3 and me["can_delegate"] is False
    assert client.get("/v1/me", headers=above).json()["can_delegate"] is True
    r = delegate(client, deepest, [cap(["list_items"], selector={"room": ["r1"]})], name="d4")
    assert r.status_code == 400 and r.json()["code"] == "depth_exceeded"
    # MCP: the tool is not even listed at the cap; calling it anyway is the same 400.
    assert "delegate" not in tools(live, deepest) and "delegate" in tools(live, above)
    res = call(live, deepest, "delegate", {"name": "d4", "capabilities": [
        cap(["list_items"], selector={"room": ["r1"]})]})
    assert res["isError"] is True and text_json(res) == r.json()


def test_names_are_namespaced_and_validated(client, parent):
    c = [cap(["list_items"], selector={"room": ["r1"]})]
    assert delegate(client, parent.headers, c, name="a/b").json()["code"] == "invalid_name"
    assert delegate(client, parent.headers, c, name="..").json()["code"] == "invalid_name"
    assert delegate(client, parent.headers, c, name="ok").status_code == 201
    r = delegate(client, parent.headers, c, name="ok")
    assert r.status_code == 409 and r.json()["code"] == "name_taken"


def test_invalid_input_is_400(client, parent):
    c = [cap(["list_items"], selector={"room": ["r1"]})]
    assert delegate(client, parent.headers, [{"target": "nope", "actions": ["x"]}]).json()[
        "code"] == "invalid_capabilities"
    assert delegate(client, parent.headers, c, role="admin").json()["code"] == "invalid_role"
    assert delegate(client, parent.headers, c, denies={"nope": {"room": ["r1"]}}).json()[
        "code"] == "invalid_denies"
    assert delegate(client, parent.headers, c, denies={"echo": {"nope": ["x"]}}).json()[
        "code"] == "invalid_denies"
    assert delegate(client, parent.headers, c, denies={"echo": {"room": ["bad"]}}).json()[
        "code"] == "invalid_denies"
    assert delegate(client, parent.headers, c, bogus=1).status_code == 422
    settings.set_enabled("echo", False)
    assert delegate(client, parent.headers, c).json()["code"] == "invalid_capabilities"


def test_a_plugin_outage_while_normalizing_denies_is_503(client, echo_local, parent,
                                                         monkeypatch):
    from aab_plugin_runtime import AdapterError

    def down(kind, value):
        raise AdapterError(503, "plugin down")
    monkeypatch.setattr(echo_local.impl, "normalize", down)
    r = delegate(client, parent.headers, [cap(["list_items"], selector={"room": ["r1"]})],
                 denies={"echo": {"room": ["r2"]}})
    assert r.status_code == 503 and r.json()["code"] == "unavailable"
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM api_keys").fetchone()[0] == 1


def test_denies_carry_over_and_only_grow(client, echo_local, make_agent):
    p = make_agent([cap(["list_items"], selector={"room": ["r1", "r2", "r3"]})],
                   denies={"echo": {"room": ["r2"]}})
    body = delegate(client, p.headers, [cap(["list_items"], selector={"room": ["r1", "r3"]})],
                    denies={"echo": {"room": [" R3 "]}}).json()     # normalized by the plugin
    child = bearer(body)
    ctx = auth.authenticate_bearer(child["Authorization"])
    assert ctx.denies == {"echo": {"room": ["r2", "r3"]}}
    assert list_room(client, child, "r1").status_code == 200
    assert list_room(client, child, "r3").status_code == 403
    # The returned capabilities already leave the child's own deny out.
    assert [c["selector"] for c in body["capabilities"]] == [{"room": ["r1"]}]
    # get_my_access never lists the deny set itself.
    assert "r3" not in client.get("/v1/me", headers=child).text


def test_a_half_made_delegation_is_undone(client, parent, monkeypatch):
    def boom(*a, **kw):
        raise ValueError("parent grant is not active")
    monkeypatch.setattr(store, "insert_child_grant", boom)
    r = delegate(client, parent.headers, [cap(["list_items"], selector={"room": ["r1"]})])
    assert r.status_code == 409 and r.json()["code"] == "conflict"
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM api_keys").fetchone()[0] == 1


# ---- revoke / list -----------------------------------------------------------------------

def test_revoking_a_child_kills_it_and_its_descendants(client, parent):
    root, child, grandchild = chain(client, parent, 2)
    child_id = auth.authenticate_bearer(child["Authorization"]).key_id
    r = client.post(f"/v1/delegations/{child_id}/revoke", headers=root)
    assert r.status_code == 200 and r.json() == {"key_id": child_id, "name": "bot/d1",
                                                 "status": "revoked"}
    assert client.get("/v1/me", headers=child).status_code == 401
    assert client.get("/v1/me", headers=grandchild).status_code == 401
    assert {g.status for g in store.list_for_key(child_id)} == {"revoked"}
    assert all(g.decided_via == "agent" for g in store.list_for_key(child_id))
    assert list_room(client, root, "r1").status_code == 200       # the parent is untouched


def test_owner_disabling_the_parent_kills_child_and_grandchild(client, admin_headers, parent):
    root, child, grandchild = chain(client, parent, 2)
    client.patch(f"/v1/admin/keys/{parent.key_id}", json={"disabled": True},
                 headers=admin_headers)
    for h in (root, child, grandchild):
        assert client.get("/v1/me", headers=h).status_code == 401


def test_lowering_the_parents_role_bounds_the_child_at_once(client, admin_headers, parent):
    child = bearer(delegate(client, parent.headers,
                            [cap(["post_item"], selector={"room": ["r1"]})]).json())
    assert post_room(client, child, "r1").status_code == 200
    client.patch(f"/v1/admin/keys/{parent.key_id}", json={"role": "read-draft"},
                 headers=admin_headers)
    # The child's own row still says "full", but every ancestor's role bounds it.
    r = post_room(client, child, "r1")
    assert r.status_code == 202 and r.json()["status"] == "pending_approval"
    client.patch(f"/v1/admin/keys/{parent.key_id}", json={"role": "read-only"},
                 headers=admin_headers)
    assert post_room(client, child, "r1").status_code == 403


def test_revoking_the_parents_grant_empties_the_subtree(client, admin_headers, parent):
    root, child, grandchild = chain(client, parent, 2)
    client.post(f"/v1/admin/grants/{parent.grant_id}/revoke", headers=admin_headers)
    for h in (child, grandchild):
        assert list_room(client, h, "r1").status_code == 403
        me = client.get("/v1/me", headers=h).json()
        assert me["targets"]["echo"]["capabilities"] == []


def test_revoke_reaches_descendants_only(client, echo_local, make_agent, parent):
    root, child, grandchild = chain(client, parent, 2)
    ids = [auth.authenticate_bearer(h["Authorization"]).key_id for h in (root, child,
                                                                          grandchild)]
    sibling = bearer(delegate(client, root, [cap(["list_items"], selector={"room": ["r1"]})],
                              name="sib").json())
    stranger = make_agent([cap(["list_items"])])
    refused = [(child, ids[0]), (grandchild, ids[1]), (child, ids[1]),     # up, self
               (sibling, ids[1]), (stranger.headers, ids[1]), (root, 99999)]
    for h, target in refused:
        r = client.post(f"/v1/delegations/{target}/revoke", headers=h)
        assert r.status_code == 404 and r.json()["code"] == "not_found", (target, r.text)
    # A grandparent may revoke a grandchild directly.
    assert client.post(f"/v1/delegations/{ids[2]}/revoke", headers=root).status_code == 200
    assert client.get("/v1/me", headers=grandchild).status_code == 401
    assert client.get("/v1/me", headers=child).status_code == 200


def test_list_my_delegations(client, parent):
    root, child, _ = chain(client, parent, 2)
    other = delegate(client, root, [cap(["list_items"], selector={"room": ["r1"]})],
                     name="other").json()
    items = client.get("/v1/delegations", headers=root).json()["items"]
    assert [(i["name"], i["status"], i["delegations"]) for i in items] == [
        ("bot/d1", "active", 1), ("bot/other", "active", 0)]
    assert items[0]["grants"][0]["capabilities"][0]["selector"] == {"room": ["r1"]}
    assert "key" not in items[0] and "denies" not in items[0]
    client.post(f"/v1/delegations/{other['key_id']}/revoke", headers=root)
    items = client.get("/v1/delegations", headers=root).json()["items"]
    assert items[1]["status"] == "disabled"
    assert items[1]["grants"][0]["status"] == "revoked"
    # Only direct children: the grandchild is counted, not listed.
    assert client.get("/v1/me", headers=root).json()["delegations"] == 1


# ---- a delegated key asking for more ------------------------------------------------------

def test_delegated_request_permission_cannot_exceed_its_parent(client, admin_headers, parent):
    child = bearer(delegate(client, parent.headers,
                            [cap(["list_items"], selector={"room": ["r1"]})]).json())
    beyond = client.post("/v1/permissions", json={"capabilities": [
        cap(["post_item"], selector={"room": ["r3"]})]}, headers=child)
    assert beyond.status_code == 400 and beyond.json()["code"] == "clipped"
    inside = client.post("/v1/permissions", json={"capabilities": [
        cap(["post_item"], selector={"room": ["r2"]})]}, headers=child)
    assert inside.status_code == 202
    client.post(f"/v1/admin/grants/{inside.json()['id']}/approve", headers=admin_headers)
    assert post_room(client, child, "r2").status_code == 200
    # The parent loses r2: the child's approved expansion shrinks with it.
    client.patch(f"/v1/admin/keys/{parent.key_id}", json={"capabilities": [
        cap(["list_items", "post_item"], selector={"room": ["r1"]})]}, headers=admin_headers)
    assert post_room(client, child, "r2").status_code == 403


# ---- the owner's view ----------------------------------------------------------------------

def test_owner_key_tree(client, admin_headers, parent):
    root, child, grandchild = chain(client, parent, 2)
    tree = client.get("/v1/admin/keys/tree", headers=admin_headers)
    assert tree.status_code == 200
    [node] = tree.json()
    assert (node["name"], node["status"], node["live"], node["depth"]) == ("bot", "active",
                                                                          True, 0)
    [c] = node["children"]
    [g] = c["children"]
    assert (c["name"], c["created_by"], c["depth"], c["live"]) == ("bot/d1", "delegation", 1,
                                                                   True)
    assert g["name"] == "bot/d1/d2" and g["grants"][0]["kind"] == "delegation"
    child_id = c["id"]
    client.post(f"/v1/delegations/{child_id}/revoke", headers=root)
    [node] = client.get("/v1/admin/keys/tree", headers=admin_headers).json()
    [c] = node["children"]
    [g] = c["children"]
    assert (c["status"], c["live"]) == ("disabled", False)
    assert (g["status"], g["live"]) == ("active", False)      # dead through its parent


def test_key_tree_needs_the_owner(client, parent):
    assert client.get("/v1/admin/keys/tree").status_code == 401
    assert client.get("/v1/admin/keys/tree", headers=parent.headers).status_code == 401


def test_key_tree_lists_orphans(client, admin_headers, parent):
    body = delegate(client, parent.headers, [cap(["list_items"], selector={"room": ["r1"]})]
                    ).json()
    with db.connect() as conn:                    # a corrupted row: the parent vanished
        conn.execute("DELETE FROM api_keys WHERE id = ?", (parent.key_id,))
    forest = client.get("/v1/admin/keys/tree", headers=admin_headers).json()
    assert [(n["id"], n.get("orphan"), n["live"]) for n in forest] == [
        (body["key_id"], True, False)]


# ---- MCP -------------------------------------------------------------------------------------

def test_delegation_tools_over_mcp_match_rest(live, parent):
    h = parent.headers
    c = [cap(["list_items"], selector={"room": ["r1"]})]
    made = call(live, h, "delegate", {"name": "viamcp", "capabilities": c})
    assert made["isError"] is False
    body = text_json(made)
    assert body["name"] == "bot/viamcp" and body["key"].startswith("aab_")
    assert list_room(live, bearer(body), "r1").status_code == 200
    assert text_json(call(live, h, "list_my_delegations")) == \
        live.get("/v1/delegations", headers=h).json()
    # The same refusal body for the same bad request.
    wide = [cap(["list_items"], selector={"room": ["r9"]})]
    res = call(live, h, "delegate", {"name": "wide", "capabilities": wide})
    assert res["isError"] is True
    assert text_json(res) == delegate(live, h, wide, name="wide").json()
    revoked = call(live, h, "revoke_delegation", {"key_id": body["key_id"]})
    assert text_json(revoked)["status"] == "revoked"
    assert live.get("/v1/me", headers=bearer(body)).status_code == 401
    res = call(live, h, "revoke_delegation", {"key_id": 99999})
    assert text_json(res) == live.post("/v1/delegations/99999/revoke", headers=h).json()
