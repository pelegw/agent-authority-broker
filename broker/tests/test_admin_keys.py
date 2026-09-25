"""Owner key management: create with root grant, list/get, patch, rotate,
and grant decisions."""

import json
import time

import pytest

from broker import auth, db
from broker.authority import store

from .conftest import cap


def create(client, headers, **kw):
    body = {"name": "bot", "role": "full", "rate_per_min": 30,
            "capabilities": [cap(["list_items", "post_item"], selector={"room": ["r1"]})], **kw}
    return client.post("/v1/admin/keys", json=body, headers=headers)


def agent_post(client, key, room="r1"):
    return client.post("/v1/targets/echo/actions/post_item",
                       json={"params": {"room": room, "text": "x"}},
                       headers={"Authorization": f"Bearer {key}"})


def test_create_key_with_root_grant_returns_plaintext_once(client, admin_headers, echo_local,
                                                           owner):
    r = create(client, admin_headers)
    assert r.status_code == 200
    body = r.json()
    key = body["key"]
    assert key.startswith("aab_") and body["grant_id"]
    g = store.get(body["grant_id"])
    assert (g.kind, g.status, g.decided_by_principal, g.decided_via) == (
        "root", "active", owner.username, "token")
    assert agent_post(client, key).status_code == 200
    # Never again: not in listings, key detail, or the audit log.
    listed = client.get("/v1/admin/keys", headers=admin_headers).text
    detail = client.get(f"/v1/admin/keys/{body['id']}", headers=admin_headers).text
    with db.connect() as conn:
        audit_text = json.dumps([dict(r) for r in conn.execute("SELECT * FROM audit_log")])
    for text in (listed, detail, audit_text):
        assert key not in text and "key_hash" not in text


def test_invalid_capabilities_create_nothing(client, admin_headers, echo_local):
    r = create(client, admin_headers, capabilities=[cap(["no_such"])])
    assert r.status_code == 400
    assert client.get("/v1/admin/keys", headers=admin_headers).json() == []


def test_grant_failure_rolls_the_key_back(client, admin_headers, echo_local, monkeypatch):
    def boom(*a, **k):
        raise ValueError("store refused")
    monkeypatch.setattr(store, "insert_root_grant", boom)
    assert create(client, admin_headers).status_code == 400
    assert client.get("/v1/admin/keys", headers=admin_headers).json() == []


def test_duplicate_name_and_bad_role_are_400(client, admin_headers, echo_local):
    create(client, admin_headers)
    assert create(client, admin_headers).status_code == 400
    assert create(client, admin_headers, name="x", role="god").status_code == 400
    past = int(time.time()) - 5
    assert create(client, admin_headers, name="y", expires_at=past).status_code == 400


def test_key_without_capabilities_has_no_grant(client, admin_headers, echo_local):
    body = create(client, admin_headers, capabilities=[]).json()
    assert body["grant_id"] is None
    assert agent_post(client, body["key"]).status_code == 403


def test_owner_key_defaults_to_the_full_ceiling(client, admin_headers, echo_local):
    """No role named: the ceiling is full, so the capabilities the owner
    ticked are exactly what the key gets (a direct write acts directly)."""
    body = {"name": "defaulted", "capabilities": [
        cap(["post_item"], selector={"room": ["r1"]}, mode="direct")]}
    r = client.post("/v1/admin/keys", json=body, headers=admin_headers).json()
    assert r["role"] == auth.OWNER_KEY_DEFAULT_ROLE == "full"
    assert client.get(f"/v1/admin/keys/{r['id']}", headers=admin_headers).json()["role"] == "full"
    assert agent_post(client, r["key"]).status_code == 200
    # The ceiling alone grants nothing: without capabilities, full does nothing.
    bare = client.post("/v1/admin/keys", json={"name": "bare"}, headers=admin_headers).json()
    assert bare["role"] == "full" and bare["grant_id"] is None
    assert agent_post(client, bare["key"]).status_code == 403


def test_a_lower_ceiling_is_still_selectable_and_caps(client, admin_headers, echo_local):
    r = create(client, admin_headers, role="read-draft", capabilities=[
        cap(["post_item"], selector={"room": ["r1"]}, mode="direct")]).json()
    assert r["role"] == "read-draft"
    assert agent_post(client, r["key"]).status_code == 202       # capped to a draft


def test_existing_keys_keep_their_stored_role(client, admin_headers, echo_local, owner):
    """The default changed for new owner keys only; a stored role is data."""
    old = auth.create_key(owner.id, "legacy", "read-only", 6, None)
    listed = {k["name"]: k["role"] for k in
              client.get("/v1/admin/keys", headers=admin_headers).json()}
    assert listed["legacy"] == "read-only"
    assert client.get(f"/v1/admin/keys/{old.key_id}",
                      headers=admin_headers).json()["role"] == "read-only"


def test_denies_are_normalized_through_the_plugin(client, admin_headers, echo_local):
    body = create(client, admin_headers, capabilities=[cap(["post_item"])],
                  denies={"echo": {"room": [" R2 "]}}).json()
    detail = client.get(f"/v1/admin/keys/{body['id']}", headers=admin_headers).json()
    assert detail["denies"] == {"echo": {"room": ["r2"]}}
    assert agent_post(client, body["key"], room="r2").status_code == 404
    bad = create(client, admin_headers, name="z", denies={"echo": {"room": ["lobby"]}})
    assert bad.status_code == 400


def test_get_key_includes_grants(client, admin_headers, echo_local):
    body = create(client, admin_headers).json()
    detail = client.get(f"/v1/admin/keys/{body['id']}", headers=admin_headers).json()
    assert [g["id"] for g in detail["grants"]] == [body["grant_id"]]
    assert detail["grants"][0]["capabilities"][0]["selector"] == {"room": ["r1"]}
    assert client.get("/v1/admin/keys/999", headers=admin_headers).status_code == 404


def test_patch_key_fields(client, admin_headers, echo_local):
    body = create(client, admin_headers).json()
    url = f"/v1/admin/keys/{body['id']}"
    later = int(time.time()) + 3600
    r = client.patch(url, json={"role": "read-only", "rate_per_min": 2, "expires_at": later},
                     headers=admin_headers).json()
    assert (r["role"], r["rate_per_min"], r["expires_at"]) == ("read-only", 2, later)
    assert agent_post(client, body["key"]).status_code == 403     # role caps writes now
    r = client.patch(url, json={"clear_expiry": True}, headers=admin_headers).json()
    assert r["expires_at"] is None
    client.patch(url, json={"disabled": True}, headers=admin_headers)
    assert agent_post(client, body["key"]).status_code == 401
    assert client.patch(url, json={}, headers=admin_headers).status_code == 400
    assert client.patch(url, json={"role": "god"}, headers=admin_headers).status_code == 400


def test_patch_capabilities_edits_the_root_grant(client, admin_headers, echo_local):
    body = create(client, admin_headers).json()
    url = f"/v1/admin/keys/{body['id']}"
    client.patch(url, json={"capabilities": [cap(["post_item"], selector={"room": ["r2"]})]},
                 headers=admin_headers)
    assert agent_post(client, body["key"], "r1").status_code == 403
    assert agent_post(client, body["key"], "r2").status_code == 200
    # Empty capabilities revoke the root grant.
    client.patch(url, json={"capabilities": []}, headers=admin_headers)
    assert agent_post(client, body["key"], "r2").status_code == 403
    # And a key without a live root grant gets a fresh one.
    client.patch(url, json={"capabilities": [cap(["post_item"])]}, headers=admin_headers)
    assert agent_post(client, body["key"], "r3").status_code == 200


def test_delegated_key_capabilities_cannot_be_edited(client, admin_headers, echo_local, owner):
    body = create(client, admin_headers).json()
    child = auth.create_key(owner.id, "child", "full", 10, None, parent_key_id=body["id"],
                            created_by="delegation")
    r = client.patch(f"/v1/admin/keys/{child.key_id}",
                     json={"capabilities": [cap(["post_item"])]}, headers=admin_headers)
    assert r.status_code == 400


def test_rotate_keeps_the_old_secret_in_grace(client, admin_headers, echo_local):
    body = create(client, admin_headers).json()
    new = client.post(f"/v1/admin/keys/{body['id']}/rotate", headers=admin_headers).json()["key"]
    assert new != body["key"]
    assert agent_post(client, new).status_code == 200
    assert agent_post(client, body["key"]).status_code == 200
    assert client.post("/v1/admin/keys/999/rotate", headers=admin_headers).status_code == 404


def test_grants_list_and_decisions(client, admin_headers, echo_local, make_agent):
    a = make_agent([cap(["list_items"])])
    gid = client.post("/v1/permissions", json={"capabilities": [cap(["post_item"])]},
                      headers=a.headers).json()["id"]
    pending = client.get("/v1/admin/grants?status=pending", headers=admin_headers).json()
    assert [g["id"] for g in pending] == [gid]
    for verb, status in (("approve", 200), ("approve", 409), ("revoke", 200), ("reject", 409)):
        assert client.post(f"/v1/admin/grants/{gid}/{verb}",
                           headers=admin_headers).status_code == status, verb
    assert client.post("/v1/admin/grants/nope/approve", headers=admin_headers).status_code == 404
    assert client.post(f"/v1/admin/grants/{gid}/bless", headers=admin_headers).status_code == 422


@pytest.mark.parametrize("path", ["/v1/admin/keys", "/v1/admin/grants"])
def test_key_routes_need_admin(client, owner, path):
    assert client.get(path).status_code == 401
