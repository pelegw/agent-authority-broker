"""REST and MCP are two views of one dispatch path; this proves they agree.

For every echo action: the same params give the same JSON over both
surfaces (modulo MCP's text wrapper), the same status semantics (200 data,
202 envelope, the same refusal body for 400/403/404/429/502/503), and the
MCP tool list is exactly the set of actions REST reports as reachable for
the key. Disabling the plugin removes its tools, and a stale tool name is
still refused at call time exactly as REST refuses it.
"""

import base64
import copy
import re

import pytest

from broker import hidden
from broker.authority import store
from broker.plugins import settings

from .conftest import cap, echo_manifest, enable_plugin
from .mcp_helpers import call, live, text_json, tools  # noqa: F401

ACT = "/v1/targets/echo/actions"
# One valid call per echo action; the first test forces this to stay complete.
CALLS = {
    "list_items": {"room": "r1"},
    "get_item": {"item_id": "i1"},
    "get_blob": {"item_id": "i2"},
    "watch": {"cursor": 2},
    "post_item": {"room": "r1", "text": "hi"},
    "delete_item": {"item_id": "i4"},
    "touch_item": {"item_id": "i1"},
}
ALL = sorted(CALLS)


def test_every_echo_action_is_covered():
    assert set(CALLS) == {a.name for a in echo_manifest().actions}


def _snapshot(impl):
    return copy.deepcopy(impl.items), impl.seq


def _restore(impl, snap):
    impl.items, impl.seq = copy.deepcopy(snap[0]), snap[1]


def _echo_tools(client, headers) -> set[str]:
    return {n.removeprefix("echo_") for n in tools(client, headers) if n.startswith("echo_")}


def _rest_reachable(client, headers) -> set[str]:
    items = client.get("/v1/targets", headers=headers).json()["items"]
    return {a for t in items if t["id"] == "echo" for a in t["actions"]}


def both(client, impl, headers, action, params, arm=lambda: None, **controls):
    """Call over REST, restore the target, call over MCP. `arm` re-applies
    any one-shot setup (an injected failure) before each call."""
    snap = _snapshot(impl)
    arm()
    rest = client.post(f"{ACT}/{action}", json={"params": params, **controls},
                       headers=headers)
    _restore(impl, snap)
    arm()
    mcp = call(client, headers, f"echo_{action}", {**params, **controls})
    _restore(impl, snap)
    return rest, mcp


def assert_same(rest, mcp):
    if rest.status_code == 200:
        assert mcp["isError"] is False
        if rest.headers["content-type"].startswith("application/json"):
            assert text_json(mcp) == rest.json()
        else:
            [block] = mcp["content"]
            res = block["resource"]
            assert base64.b64decode(res["blob"]) == rest.content
            assert res["mimeType"] == rest.headers["content-type"]
    elif rest.status_code == 202:
        assert mcp["isError"] is False
        body = text_json(mcp)
        assert set(body) == set(rest.json()) and body["status"] == rest.json()["status"]
    else:
        assert mcp["isError"] is True
        assert text_json(mcp) == rest.json()


# ------------------------------------------------------------ same result

@pytest.mark.parametrize("action", ALL)
def test_same_result_over_rest_and_mcp(live, echo, make_agent, action):
    a = make_agent([cap(ALL)])
    rest, mcp = both(live, echo.impl, a.headers, action, CALLS[action])
    assert rest.status_code == 200, rest.text
    assert_same(rest, mcp)


# ------------------------------------------------------------ same status semantics

CASES = {
    # name: (status, action, params, controls, capability overrides)
    "allow": (200, "post_item", {"room": "r1", "text": "x"}, {}, {}),
    "as_draft": (202, "post_item", {"room": "r1", "text": "x"}, {"as_draft": True}, {}),
    "scheduled": (202, "post_item", {"room": "r1", "text": "x"}, {"delay_seconds": 300}, {}),
    "draft_mode_cap": (202, "post_item", {"room": "r1", "text": "x"}, {}, {"mode": "draft"}),
    "out_of_grant": (403, "post_item", {"room": "r2", "text": "x"}, {}, {}),
    "action_not_granted": (403, "delete_item", {"item_id": "i1"}, {}, {}),
    "mode_blocked": (403, "touch_item", {"item_id": "i1"}, {}, {"mode": "draft"}),
    "hidden": (404, "get_item", {"item_id": "i2"}, {}, {}),
    "unknown_action": (404, "nope", {}, {}, {}),
    "disabled": (404, "list_items", {}, {}, {}),
    "bad_params": (400, "post_item", {"room": "r1", "text": ""}, {}, {}),
    "unknown_param": (400, "list_items", {"bogus": 1}, {}, {}),
    "draft_unsupported": (400, "list_items", {}, {"as_draft": True}, {}),
    "both_schedules": (400, "post_item", {"room": "r1", "text": "x"},
                       {"run_at": 2_000_000_000, "delay_seconds": 300}, {}),
    "budget": (429, "post_item", {"room": "r1", "text": "x"}, {}, {"budget": {"per_day": 0}}),
    "not_connected": (503, "list_items", {}, {}, {}),
    "fail503": (503, "post_item", {"room": "r1", "text": "x"}, {}, {}),
    "fail502": (502, "post_item", {"room": "r1", "text": "x"}, {}, {}),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_same_status_semantics(live, echo, make_agent, name):
    status, action, params, controls, overrides = CASES[name]
    actions = ["post_item", "list_items", "get_item", "touch_item"]
    a = make_agent([cap(actions, selector={"room": ["r1"]}, **overrides)])
    arm = lambda: None  # noqa: E731
    if name == "hidden":
        hidden.add("echo", "item", "i2")
    elif name == "disabled":
        settings.set_enabled("echo", False)
    elif name == "not_connected":
        enable_plugin(connected=False)
    elif name.startswith("fail"):
        def arm():
            echo.impl.fail_next = int(name[4:])
    rest, mcp = both(live, echo.impl, a.headers, action, params, arm, **controls)
    assert rest.status_code == status, rest.text
    assert_same(rest, mcp)


# ------------------------------------------------------------ same reachable set

KEYS = {
    "full": ([cap(ALL)], "full"),
    "subset": ([cap(["list_items", "post_item"])], "full"),
    "selector": ([cap(["list_items", "post_item"], selector={"room": ["r1"]})], "full"),
    "draft_cap": ([cap(ALL, mode="draft")], "full"),
    "read_only_role": ([cap(ALL)], "read-only"),
    "read_draft_role": ([cap(ALL)], "read-draft"),
    "read_act_role": ([cap(ALL)], "read-act"),
    "no_grant": (None, "full"),
}


@pytest.mark.parametrize("key", sorted(KEYS))
def test_tool_list_equals_rest_reachable(live, echo_local, make_agent, key):
    caps, role = KEYS[key]
    a = make_agent(caps, role=role)
    listed = _echo_tools(live, a.headers)
    assert listed == _rest_reachable(live, a.headers)
    paths = live.get("/v1/me/openapi.json", headers=a.headers).json()["paths"]
    assert listed == {m.group(1) for p in paths
                      if (m := re.fullmatch(r"/v1/targets/echo/actions/(\w+)", p))}
    # The listing is honest both ways: every listed action is performable
    # over REST (200/202), every other one is refused as out of grant.
    for action in ALL:
        snap = _snapshot(echo_local.impl)
        r = live.post(f"{ACT}/{action}", json={"params": CALLS[action]}, headers=a.headers)
        _restore(echo_local.impl, snap)
        if action in listed:
            assert r.status_code in (200, 202), (action, r.text)
        else:
            assert r.status_code == 403, (action, r.text)


def test_role_and_mode_shape_the_tool_list(live, echo_local, make_agent):
    reads = {"list_items", "get_item", "get_blob", "watch"}
    assert _echo_tools(live, make_agent([cap(ALL)], role="read-only").headers) == reads
    # A draft-only authority cannot reach a write that cannot be drafted.
    assert "touch_item" not in _echo_tools(live, make_agent([cap(ALL, mode="draft")]).headers)
    assert "touch_item" not in _echo_tools(
        live, make_agent([cap(ALL)], role="read-draft").headers)


# ------------------------------------------------------------ disable / stale names

def test_disabling_the_plugin_removes_its_tools(live, echo_local, make_agent):
    a = make_agent([cap(ALL)])
    assert _echo_tools(live, a.headers) == set(ALL)
    settings.set_enabled("echo", False)
    assert _echo_tools(live, a.headers) == set()
    # The stale name is still refused at call time, as REST refuses it.
    rest, mcp = both(live, echo_local.impl, a.headers, "list_items", {})
    assert rest.status_code == 404
    assert_same(rest, mcp)
    settings.set_enabled("echo", True)
    assert _echo_tools(live, a.headers) == set(ALL)


def test_key_without_the_capability_does_not_see_the_tool(live, echo_local, make_agent):
    a = make_agent([cap(["list_items"])])
    assert _echo_tools(live, a.headers) == {"list_items"}
    rest, mcp = both(live, echo_local.impl, a.headers, "post_item", {"room": "r1", "text": "x"})
    assert rest.status_code == 403
    assert_same(rest, mcp)


def test_revoked_grant_denies_a_stale_tool_name(live, echo_local, make_agent):
    a = make_agent([cap(["post_item"])])
    assert _echo_tools(live, a.headers) == {"post_item"}
    assert store.set_status(a.grant_id, "revoked", decided_via="system")
    assert _echo_tools(live, a.headers) == set()
    rest, mcp = both(live, echo_local.impl, a.headers, "post_item", {"room": "r1", "text": "x"})
    assert rest.status_code == 403
    assert_same(rest, mcp)


# ------------------------------------------------------------ generic tools

def test_generic_tools_match_their_rest_routes(live, echo_local, make_agent):
    a = make_agent([cap(["list_items", "post_item"], selector={"room": ["r1", "r2"]})])
    h = a.headers
    assert text_json(call(live, h, "get_my_access")) == live.get("/v1/me", headers=h).json()
    assert text_json(call(live, h, "list_targets")) == live.get("/v1/targets", headers=h).json()
    assert text_json(call(live, h, "resolve_resource",
                          {"target": "echo", "kind": "room", "query": "room"})) == \
        live.get("/v1/targets/echo/resolve?kind=room&q=room", headers=h).json()
    queued = live.post(f"{ACT}/post_item", json={"params": {"room": "r1", "text": "x"},
                                                 "as_draft": True}, headers=h).json()
    aid = queued["action_id"]
    assert text_json(call(live, h, "get_action_status", {"action_id": aid})) == \
        live.get(f"/v1/actions/{aid}", headers=h).json()
    assert text_json(call(live, h, "list_my_actions")) == \
        live.get("/v1/actions", headers=h).json()
    assert text_json(call(live, h, "list_my_permissions")) == \
        live.get("/v1/permissions", headers=h).json()
    assert text_json(call(live, h, "get_permission_status", {"grant_id": a.grant_id})) == \
        live.get(f"/v1/permissions/{a.grant_id}", headers=h).json()
    # Refusals match too (resolve on an unknown target; another key's action).
    r = call(live, h, "resolve_resource", {"target": "nope", "kind": "room", "query": ""})
    assert text_json(r) == live.get("/v1/targets/nope/resolve?kind=room", headers=h).json()
    other = make_agent().headers
    assert text_json(call(live, other, "cancel_action", {"action_id": aid})) == \
        live.delete(f"/v1/actions/{aid}", headers=other).json()
