"""Scope expansion: request_permission, clipping, approval, delegated keys."""

import time
import types

import pytest

from broker import auth, notify
from broker.authority import store
from broker.authority.capability import from_json, normalize_all
from broker.authority.grant import narrow
from broker.plugins.registry import get_registry

from .conftest import cap


def perform(client, agent, action="post_item", params=None):
    return client.post(f"/v1/targets/echo/actions/{action}",
                       json={"params": params or {"room": "r1", "text": "x"}},
                       headers=agent.headers)


def request(client, agent, caps, **kw):
    return client.post("/v1/permissions", json={"capabilities": caps, **kw},
                       headers=agent.headers)


@pytest.fixture()
def child(echo_local, make_agent, owner):
    """A parent key with rooms r1,r2 and a delegated child holding only r1."""
    parent = make_agent([cap(["list_items", "post_item"], selector={"room": ["r1", "r2"]})])
    new = auth.create_key(owner.id, "child", "full", 60, None, parent_key_id=parent.key_id,
                          created_by="delegation")
    req = normalize_all([from_json(cap(["list_items"], selector={"room": ["r1"]}))],
                        get_registry().manifests())
    store.insert_child_grant(owner.id, new.key_id, "delegation",
                             narrow(store.get(parent.grant_id), req, get_registry().lattice()),
                             "sub-agent", None, parent.key_id)
    return types.SimpleNamespace(parent=parent, key_id=new.key_id,
                                 headers={"Authorization": f"Bearer {new.plaintext}"})


def test_pending_then_approve_grows_effective(client, admin_headers, echo_local, make_agent,
                                              monkeypatch):
    got = []

    class Provider:
        def notify_grant_request(self, g):
            got.append(g)

    monkeypatch.setattr(notify, "_PROVIDERS", [Provider()])
    a = make_agent([cap(["list_items"])])
    assert perform(client, a).status_code == 403
    r = request(client, a, [cap(["post_item"], selector={"room": ["r1"]})],
                reason="need to post")
    assert r.status_code == 202 and r.json()["status"] == "pending"
    gid = r.json()["id"]
    assert got[0]["id"] == gid and got[0]["reason"] == "need to post"
    assert client.get(f"/v1/permissions/{gid}", headers=a.headers).json()["status"] == "pending"
    assert perform(client, a).status_code == 403                  # pending grants nothing
    ok = client.post(f"/v1/admin/grants/{gid}/approve", headers=admin_headers)
    assert ok.status_code == 200 and ok.json()["decided_via"] == "token"
    assert perform(client, a).status_code == 200
    assert perform(client, a, params={"room": "r2", "text": "x"}).status_code == 403


def test_reject_and_revoke(client, admin_headers, echo_local, make_agent):
    a = make_agent([cap(["list_items"])])
    gid = request(client, a, [cap(["post_item"])]).json()["id"]
    assert client.post(f"/v1/admin/grants/{gid}/reject", headers=admin_headers).json()[
        "status"] == "rejected"
    assert client.post(f"/v1/admin/grants/{gid}/approve",
                       headers=admin_headers).status_code == 409
    gid2 = request(client, a, [cap(["post_item"])]).json()["id"]
    client.post(f"/v1/admin/grants/{gid2}/approve", headers=admin_headers)
    assert perform(client, a).status_code == 200
    client.post(f"/v1/admin/grants/{gid2}/revoke", headers=admin_headers)
    assert perform(client, a).status_code == 403


def test_invalid_requests_are_400(client, echo_local, make_agent):
    a = make_agent([cap(["list_items"])])
    assert request(client, a, [{"target": "nope", "actions": ["x"]}]).status_code == 400
    assert request(client, a, [cap(["no_such_action"])]).status_code == 400
    assert request(client, a, [cap(["post_item"], bogus=1)]).status_code == 400
    assert request(client, a, [cap(["post_item"])], expires_in_hours=0).status_code == 400
    assert client.post("/v1/permissions", json={"capabilities": []},
                       headers=a.headers).status_code == 422


def test_expiry_is_capped(client, echo_local, make_agent):
    a = make_agent([cap(["list_items"])])
    gid = request(client, a, [cap(["post_item"])], expires_in_hours=10**6).json()["id"]
    g = store.get(gid)
    assert g.expires_at <= int(time.time()) + 24 * 30 * 3600 + 5


def test_status_and_list_are_per_key(client, echo_local, make_agent):
    a = make_agent([cap(["list_items"])])
    b = make_agent([cap(["list_items"])])
    gid = request(client, a, [cap(["post_item"])]).json()["id"]
    assert client.get(f"/v1/permissions/{gid}", headers=b.headers).status_code == 404
    mine = client.get("/v1/permissions", headers=a.headers).json()["items"]
    assert gid in [g["id"] for g in mine] and a.grant_id in [g["id"] for g in mine]
    assert client.get("/v1/permissions", headers=b.headers).json()["items"][0]["id"] == \
        b.grant_id


def test_delegated_key_clipped_to_parent_is_400_with_the_clipped_list(client, child):
    wide = cap(["post_item"], selector={"room": ["r1", "r3"]})
    r = request(client, child, [wide])
    assert r.status_code == 400
    body = r.json()
    assert body["code"] == "clipped"
    assert body["clipped"] == [{
        "target": "echo", "actions": ["post_item"], "selector": {"room": ["r1", "r3"]},
        "constraints": {}, "mode": "direct", "expires_at": None, "budget": {}}]
    assert body["allowed"][0]["selector"] == {"room": ["r1"]}


def test_delegated_key_request_inside_parent_goes_pending(client, admin_headers, child):
    r = request(client, child, [cap(["post_item"], selector={"room": ["r2"]})])
    assert r.status_code == 202
    g = store.get(r.json()["id"])
    assert (g.kind, g.status, g.parent_grant_id) == ("expansion", "pending",
                                                     child.parent.grant_id)
    client.post(f"/v1/admin/grants/{g.id}/approve", headers=admin_headers)
    ok = client.post("/v1/targets/echo/actions/post_item",
                     json={"params": {"room": "r2", "text": "x"}}, headers=child.headers)
    assert ok.status_code == 200


def test_delegated_key_never_exceeds_parent_after_parent_narrows(client, admin_headers, child):
    gid = request(client, child, [cap(["post_item"], selector={"room": ["r2"]})]).json()["id"]
    client.post(f"/v1/admin/grants/{gid}/approve", headers=admin_headers)
    # The owner narrows the parent's root grant; the child's approved
    # expansion shrinks with it on the next call (chain meet).
    client.patch(f"/v1/admin/keys/{child.parent.key_id}",
                 json={"capabilities": [cap(["list_items", "post_item"],
                                            selector={"room": ["r1"]})]},
                 headers=admin_headers)
    r = client.post("/v1/targets/echo/actions/post_item",
                    json={"params": {"room": "r2", "text": "x"}}, headers=child.headers)
    assert r.status_code == 403
