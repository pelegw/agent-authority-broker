"""The role as a ceiling, where people and agents see it.

`get_my_access` reports the key's `ceiling` and, per capability, the actions
it lowers (`effective_mode`); the owner's pending-grant list and the Telegram
approval card say when a request would be capped, because approving it then
does not do what the agent asked. Every claim is checked against what the
engine really does with the same key.
"""

import json

from broker import auth
from broker.notify import cards

from .conftest import cap
from .mcp_helpers import call, live, text_json, tools  # noqa: F401

POST = "/v1/targets/echo/actions/post_item"


def post(client, headers, room="r1"):
    return client.post(POST, json={"params": {"room": room, "text": "x"}}, headers=headers)


def me(client, headers) -> dict:
    return client.get("/v1/me", headers=headers).json()


def request_direct_post(client, agent) -> str:
    r = client.post("/v1/permissions", headers=agent.headers, json={
        "capabilities": [cap(["post_item"], selector={"room": ["r1"]}, mode="direct")],
        "reason": "need to post"})
    assert r.status_code == 202, r.text
    return r.json()["id"]


# ---- get_my_access ------------------------------------------------------------------

def test_me_says_the_capability_is_direct_but_the_ceiling_drafts(client, echo_local, make_agent):
    a = make_agent([cap(["list_items", "post_item"], mode="direct")], role="read-draft")
    body = me(client, a.headers)
    assert (body["role"], body["ceiling"]) == ("read-draft", "read-draft")
    [c] = body["targets"]["echo"]["capabilities"]
    assert c["mode"] == "direct"                        # what the grant says
    assert c["effective_mode"] == {"post_item": "draft"}   # what a call really does
    assert c["grant_chain"] == [a.grant_id]
    # And that is what happens.
    r = post(client, a.headers)
    assert r.status_code == 202 and r.json()["status"] == "pending_approval"


def test_me_under_full_has_no_effective_mode(client, echo_local, make_agent):
    a = make_agent([cap(["list_items", "post_item"], mode="direct")])
    body = me(client, a.headers)
    assert body["ceiling"] == "full"
    [c] = body["targets"]["echo"]["capabilities"]
    assert "effective_mode" not in c and c["mode"] == "direct"
    assert post(client, a.headers).status_code == 200


def test_me_marks_denied_writes_and_the_tool_list_leaves_them_out(client, echo_local,
                                                                  make_agent):
    a = make_agent([cap(["list_items", "post_item", "delete_item"], mode="direct")],
                   role="read-only")
    body = me(client, a.headers)
    [c] = body["targets"]["echo"]["capabilities"]
    assert c["effective_mode"] == {"delete_item": "denied", "post_item": "denied"}
    # The capability is still listed (it is the agent's own grant), but the
    # surfaces that offer actions never offer what the ceiling denies.
    [echo] = client.get("/v1/targets", headers=a.headers).json()["items"]
    assert echo["actions"] == ["list_items"]
    assert post(client, a.headers).status_code == 403
    # Limits of denied actions are not reported as enforced anywhere.
    assert "attachments" not in body["targets"]["echo"]["enforced_where"]


def test_me_ceiling_comes_from_the_lowest_key_in_the_chain(client, admin_headers, echo_local,
                                                           make_agent):
    parent = make_agent([cap(["list_items", "post_item"], mode="direct")], name="bot")
    r = client.post("/v1/delegations", headers=parent.headers, json={
        "name": "helper", "capabilities": [cap(["post_item"], mode="direct")]})
    assert r.status_code == 201, r.text
    child = {"Authorization": f"Bearer {r.json()['key']}"}
    assert me(client, child)["ceiling"] == "full"
    client.patch(f"/v1/admin/keys/{parent.key_id}", json={"role": "read-draft"},
                 headers=admin_headers)
    body = me(client, child)
    assert (body["role"], body["ceiling"]) == ("full", "read-draft")
    [c] = body["targets"]["echo"]["capabilities"]
    assert c["effective_mode"] == {"post_item": "draft"}
    assert post(client, child).status_code == 202


def test_delegation_still_defaults_to_the_callers_role(client, echo_local, make_agent):
    """The owner default (full) is for owner-created keys only: a delegated
    key without a role gets its caller's, and can never exceed it."""
    parent = make_agent([cap(["list_items"])], role="read-draft", name="drafty")
    r = client.post("/v1/delegations", headers=parent.headers, json={
        "name": "helper", "capabilities": [cap(["list_items"])]})
    assert r.status_code == 201 and r.json()["role"] == "read-draft"
    r = client.post("/v1/delegations", headers=parent.headers, json={
        "name": "greedy", "capabilities": [cap(["list_items"])], "role": "full"})
    assert r.status_code == 400 and r.json()["code"] == "exceeds_parent"


def test_me_never_shows_what_hiding_removed(client, echo_local, make_agent):
    """The uncapped view goes through the same visibility filter."""
    from broker import hidden
    a = make_agent([cap(["post_item"], selector={"room": ["r1", "r2"]}, mode="direct")],
                   role="read-draft")
    hidden.add("echo", "room", "r2", label="Secret Room")
    text = json.dumps(me(client, a.headers))
    assert "r2" not in text and "Secret Room" not in text


def test_mcp_get_my_access_carries_the_same_ceiling(live, echo_local, make_agent):
    a = make_agent([cap(["post_item"], mode="direct")], role="read-draft")
    assert "echo_post_item" in tools(live, a.headers)       # a draft can still be queued
    body = text_json(call(live, a.headers, "get_my_access"))
    assert body == live.get("/v1/me", headers=a.headers).json()
    assert body["ceiling"] == "read-draft"


def test_key_skill_copy_says_what_the_ceiling_caps(client, echo_local, make_agent):
    a = make_agent([cap(["post_item"], selector={"room": ["r1"]}, mode="direct")],
                   role="read-draft", name="poster")
    text = client.get("/v1/me/skill", headers=a.headers).text
    assert "- Key `poster`: ceiling `read-draft`," in text
    assert "capped by your ceiling: `post_item` draft" in text


# ---- the owner's pending grants and the Telegram card ------------------------------------

def test_pending_grant_says_the_ceiling_will_cap_it(client, admin_headers, echo_local,
                                                    make_agent):
    a = make_agent([cap(["list_items"])], role="read-draft", name="drafty")
    gid = request_direct_post(client, a)
    [g] = client.get("/v1/admin/grants?status=pending", headers=admin_headers).json()
    assert g["id"] == gid and g["ceiling"] == "read-draft"
    assert "ceiling is read-draft" in g["ceiling_note"]
    assert "post_item will still queue for your approval" in g["ceiling_note"]
    # The note is true: approved, the call still queues.
    client.post(f"/v1/admin/grants/{gid}/approve", headers=admin_headers)
    assert post(client, a.headers).status_code == 202


def test_pending_grant_under_full_carries_no_note(client, admin_headers, echo_local, make_agent):
    a = make_agent([cap(["list_items"])])
    request_direct_post(client, a)
    [g] = client.get("/v1/admin/grants?status=pending", headers=admin_headers).json()
    assert (g["ceiling"], g["ceiling_note"]) == ("full", None)


def test_a_draft_request_under_read_draft_is_not_capped(client, admin_headers, echo_local,
                                                        make_agent):
    a = make_agent([cap(["list_items"])], role="read-draft")
    client.post("/v1/permissions", headers=a.headers,
                json={"capabilities": [cap(["post_item"])]})      # no mode: draft
    [g] = client.get("/v1/admin/grants?status=pending", headers=admin_headers).json()
    assert g["ceiling_note"] is None


def test_read_only_key_asking_to_write_is_told_it_stays_denied(client, admin_headers,
                                                               echo_local, make_agent):
    a = make_agent([cap(["list_items"])], role="read-only")
    request_direct_post(client, a)
    [g] = client.get("/v1/admin/grants?status=pending", headers=admin_headers).json()
    assert "post_item will stay denied" in g["ceiling_note"]


def test_telegram_card_warns_when_the_ceiling_caps_the_request(client, echo_local, make_agent,
                                                               fake_telegram):
    a = make_agent([cap(["list_items"])], role="read-draft", name="drafty")
    gid = request_direct_post(client, a)
    [card] = fake_telegram["sent"]
    assert "ceiling is read-draft" in card["text"]
    assert "post_item will still queue for your approval" in card["text"]
    assert card["keyboard"] is not None                  # still approvable, just honest
    # The tap-time re-render (the oversized check) produces the same card.
    from broker.authority import store
    assert cards.grant_card(cards.grant_from_store(store.get(gid))).text == card["text"]


def test_telegram_card_under_full_has_no_warning(client, echo_local, make_agent, fake_telegram):
    a = make_agent([cap(["list_items"])], name="free")
    request_direct_post(client, a)
    [card] = fake_telegram["sent"]
    assert "ceiling" not in card["text"]


def test_card_note_follows_the_delegation_chain(echo_local, make_agent, owner, admin_headers,
                                                client):
    parent = make_agent([cap(["post_item"], mode="direct")], role="read-act", name="planner")
    child = auth.create_key(owner.id, "planner/helper", "read-act", 60, None,
                            parent_key_id=parent.key_id, created_by="delegation")
    client.patch(f"/v1/admin/keys/{parent.key_id}", json={"role": "read-draft"},
                 headers=admin_headers)
    text = cards.grant_text({"id": "g", "key_id": child.key_id, "created_at": 1,
                             "expires_at": None,
                             "capabilities": [cap(["post_item"], mode="direct")]})
    assert "ceiling is read-draft" in text
    # A key whose chain is broken claims nothing.
    assert "ceiling" not in cards.grant_text({"id": "g", "key_id": 99999, "created_at": 1,
                                              "expires_at": None, "capabilities": []})
