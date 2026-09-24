"""Hidden resources: admin management, and hidden == 404 on every surface."""

import json

import pytest

from broker import engine, hidden
from broker.actions import queue, scheduler
from broker.config import get_settings

from .conftest import cap

ACT = "/v1/targets/echo/actions"
NOT_FOUND = {"error": "not found", "code": "not_found"}


def hide(client, headers, kind, rid, **kw):
    return client.post("/v1/admin/hidden", json={"target": "echo", "kind": kind,
                                                 "resource_id": rid, **kw}, headers=headers)


def test_admin_add_list_remove(client, admin_headers, echo_local, owner):
    r = hide(client, admin_headers, "room", " R2 ", reason="family")
    assert r.status_code == 200
    assert r.json() == {"target": "echo", "kind": "room", "resource_id": "r2",
                        "label": "Room Two", "reason": "family"}
    listed = client.get("/v1/admin/hidden", headers=admin_headers).json()
    assert [(h["kind"], h["resource_id"]) for h in listed] == [("room", "r2")]
    d = client.delete("/v1/admin/hidden/echo/room/r2", headers=admin_headers)
    assert d.json()["removed"] is True
    assert client.delete("/v1/admin/hidden/echo/room/r2", headers=admin_headers).status_code == 404


def test_admin_validation(client, admin_headers, echo_local):
    assert hide(client, admin_headers, "planet", "x").status_code == 400
    assert hide(client, admin_headers, "room", "lobby").status_code == 400   # not a room id
    r = client.post("/v1/admin/hidden", json={"target": "nope", "kind": "room",
                                              "resource_id": "r1"}, headers=admin_headers)
    assert r.status_code == 404
    assert client.get("/v1/admin/hidden").status_code == 401


def test_label_is_display_only(client, admin_headers, echo_local, make_agent):
    a = make_agent([cap(["get_item"])])
    hide(client, admin_headers, "item", "i1", label="an old name")
    hidden.add("echo", "item", "i1", label="renamed")          # relabel: still hidden
    r = client.post(f"{ACT}/get_item", json={"params": {"item_id": "i1"}}, headers=a.headers)
    assert r.status_code == 404


def test_hidden_equals_missing_on_get(client, admin_headers, echo, make_agent):
    a = make_agent([cap(["get_item", "get_blob", "delete_item", "touch_item"])])
    hide(client, admin_headers, "item", "i1")
    for action in ("get_item", "get_blob", "delete_item", "touch_item"):
        hid = client.post(f"{ACT}/{action}", json={"params": {"item_id": "i1"}},
                          headers=a.headers)
        missing = client.post(f"{ACT}/{action}", json={"params": {"item_id": "zz"}},
                              headers=a.headers)
        assert hid.status_code == missing.status_code == 404, action
        assert hid.json() == missing.json() == NOT_FOUND
    assert "i1" in echo.impl.items                              # delete never reached it


def test_hidden_filtered_from_lists(client, admin_headers, echo, make_agent):
    a = make_agent([cap(["list_items"])])
    hide(client, admin_headers, "room", "r1")
    r = client.post(f"{ACT}/list_items", json={"params": {}}, headers=a.headers).json()
    assert [i["id"] for i in r["items"]] == ["i2", "i4"]
    # Asking for the hidden room by id is the same 404 as a missing resource.
    r = client.post(f"{ACT}/list_items", json={"params": {"room": "r1"}}, headers=a.headers)
    assert r.status_code == 404 and r.json() == NOT_FOUND


def test_hidden_folder_hides_its_subtree(client, admin_headers, echo, make_agent):
    a = make_agent([cap(["list_items", "get_item"])])
    hide(client, admin_headers, "folder", "a")
    r = client.post(f"{ACT}/list_items", json={"params": {}}, headers=a.headers).json()
    assert [i["id"] for i in r["items"]] == ["i2"]              # a1, a1x, a2 are under a
    r = client.post(f"{ACT}/get_item", json={"params": {"item_id": "i3"}}, headers=a.headers)
    assert r.json() == NOT_FOUND


def test_hidden_filtered_from_long_poll(client, admin_headers, echo_local, make_agent,
                                        monkeypatch):
    monkeypatch.setenv("LONG_POLL_INTERVAL_SECONDS", "0.05")
    get_settings.cache_clear()
    a = make_agent([cap(["watch"])])
    hide(client, admin_headers, "item", "i2")
    r = client.get(f"{ACT}/watch?cursor=0", headers=a.headers).json()
    assert "i2" not in [i["id"] for i in r["items"]]


def test_hidden_filtered_from_resolve_and_me(client, admin_headers, echo_local, make_agent):
    a = make_agent([cap(["list_items"], selector={"room": ["r1", "r2"]})])
    hide(client, admin_headers, "room", "r2")
    r = client.get("/v1/targets/echo/resolve?kind=room&q=r", headers=a.headers).json()
    assert [i["id"] for i in r["items"]] == ["r1"]
    me = client.get("/v1/me", headers=a.headers).json()
    assert "r2" not in json.dumps(me)


def test_admin_resolve_sees_everything(client, admin_headers, echo_local):
    hidden.add("echo", "room", "r2")
    r = client.get("/v1/admin/resolve?target=echo&kind=room&q=room", headers=admin_headers)
    assert [i["id"] for i in r.json()] == ["r1", "r2", "r3", "r4"]


def test_hiding_after_queuing_blocks_delivery(client, admin_headers, echo_local, make_agent):
    drafter = make_agent([cap(["post_item"], mode="draft")])
    scheduled = make_agent([cap(["post_item"])])
    d = engine.perform(drafter.auth, "echo", "post_item", {"room": "r3", "text": "x"})
    s = engine.perform(scheduled.auth, "echo", "post_item", {"room": "r3", "text": "x"},
                       delay_seconds=120)
    hide(client, admin_headers, "room", "r3")
    r = client.post(f"/v1/admin/actions/{d.body['action_id']}/approve", headers=admin_headers)
    assert r.status_code == 404 and r.json() == NOT_FOUND
    from broker import db
    with db.connect() as conn:
        conn.execute("UPDATE actions SET run_at = 1 WHERE id = ?", (s.body["action_id"],))
    scheduler._tick()
    assert queue.get_row(s.body["action_id"])["status"] == "canceled"
    assert echo_local.impl.calls == []


@pytest.mark.parametrize("surface", ["perform", "list"])
def test_key_denies_behave_like_hidden(client, echo_local, make_agent, surface):
    a = make_agent([cap(["get_item", "list_items"])], denies={"echo": {"item": ["i4"]}})
    if surface == "perform":
        r = client.post(f"{ACT}/get_item", json={"params": {"item_id": "i4"}},
                        headers=a.headers)
        assert r.json() == NOT_FOUND
    else:
        r = client.post(f"{ACT}/list_items", json={"params": {}}, headers=a.headers).json()
        assert "i4" not in [i["id"] for i in r["items"]]
