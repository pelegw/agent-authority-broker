"""The MCP surface: tool registry, auth middleware, DNS-rebinding check, the
live streamable-HTTP handshake, blocking work off the loop, binary results
and isError bodies. (Parity with REST is tests/test_parity.py.)"""

import asyncio
import base64
import inspect
import json

import pytest
from fastapi.testclient import TestClient

from broker import engine, mcp_generic, mcp_server, mcp_tools
from broker.errors import PolicyError
from broker.plugins.manifest import Action

from .conftest import cap
from .mcp_helpers import ACCEPT, call, live, rpc, text_json, tools  # noqa: F401

GENERIC = {"get_my_access", "list_targets", "resolve_resource", "request_permission",
           "get_permission_status", "list_my_permissions", "get_action_status",
           "list_my_actions", "cancel_action"}
ALL_ECHO = ["list_items", "get_item", "get_blob", "watch", "post_item", "delete_item",
            "touch_item"]
INITIALIZE = {"protocolVersion": "2025-03-26", "capabilities": {},
              "clientInfo": {"name": "pytest", "version": "0"}}


# ------------------------------------------------------------ registry

def test_generic_tools_match_contract_and_have_no_approve(live, echo_local, make_agent):
    a = make_agent([cap(ALL_ECHO)])
    names = set(tools(live, a.headers))
    assert names == GENERIC | {f"echo_{x}" for x in ALL_ECHO}
    # Approval is a human act: no approve/reject tool may ever appear.
    assert not any("approve" in n or "reject" in n for n in names)
    assert set(mcp_generic.BY_NAME) == GENERIC


def test_key_without_grants_sees_only_generic_tools(live, echo_local, make_agent):
    assert set(tools(live, make_agent().headers)) == GENERIC


def test_tool_list_is_per_caller(live, echo_local, make_agent):
    a = make_agent([cap(["list_items"])])
    b = make_agent([cap(["post_item"])])
    assert set(tools(live, a.headers)) - GENERIC == {"echo_list_items"}
    assert set(tools(live, b.headers)) - GENERIC == {"echo_post_item"}
    assert set(tools(live, a.headers)) - GENERIC == {"echo_list_items"}


def test_handlers_are_async():
    # A sync handler would run inline on the event loop and freeze the broker
    # for the duration of a plugin call.
    assert inspect.iscoroutinefunction(mcp_server.list_tools)
    assert inspect.iscoroutinefunction(mcp_server.call_tool)


def test_action_tool_schema_controls_and_hints(live, echo_local, make_agent):
    t = tools(live, make_agent([cap(ALL_ECHO)]).headers)
    post = t["echo_post_item"]
    props = post["inputSchema"]["properties"]
    assert {"room", "text", "as_draft", "run_at", "delay_seconds", "note"} <= set(props)
    assert post["inputSchema"]["required"] == ["room", "text"]
    assert post["inputSchema"]["additionalProperties"] is False
    assert "pending_approval" in post["description"]
    assert "scheduled" in post["description"]
    # A read takes no controls; a drafts-capable destructive takes as_draft
    # and note but cannot be scheduled; a direct-only write takes none.
    assert set(t["echo_list_items"]["inputSchema"]["properties"]) == {"room", "limit"}
    assert set(t["echo_delete_item"]["inputSchema"]["properties"]) == \
        {"item_id", "as_draft", "note"}
    assert set(t["echo_touch_item"]["inputSchema"]["properties"]) == {"item_id"}
    assert t["echo_list_items"]["annotations"]["readOnlyHint"] is True
    assert t["echo_delete_item"]["annotations"]["destructiveHint"] is True
    assert "base64" in t["echo_get_blob"]["description"]
    assert "wait=" in t["echo_watch"]["description"]


def test_generic_tool_schemas_are_strict_and_compact(live, echo_local, make_agent):
    t = tools(live, make_agent().headers)
    for name in GENERIC:
        schema = t[name]["inputSchema"]
        assert schema["type"] == "object" and schema.get("additionalProperties") is False
        assert "title" not in json.dumps(schema)
    assert t["request_permission"]["inputSchema"]["required"] == ["capabilities"]


def test_tool_name_mapping():
    assert mcp_tools.split_tool_name("echo_post_item") == ("echo", "post_item")
    assert mcp_tools.split_tool_name("whatsapp_send_message") == ("whatsapp", "send_message")
    for bad in ("echo", "echo_", "_post", "Echo_post", "echo_Post", "e-x_y", "echo.post_item"):
        assert mcp_tools.split_tool_name(bad) is None, bad


def test_a_param_named_like_a_control_wins():
    act = Action(name="x", side_effect="write", schedulable=True,
                 params={"type": "object", "properties": {"note": {"type": "string"}}})
    assert mcp_tools.controls_for(act) == ["as_draft", "run_at", "delay_seconds"]
    params, controls = mcp_tools._split_controls(act, {"note": "a param", "as_draft": True})
    assert params == {"note": "a param"} and controls["as_draft"] is True
    assert controls["note"] == ""


# ------------------------------------------------------------ auth middleware

def test_mcp_requires_a_key_and_answers_without_redirect(env, admin_headers):
    # No lifespan needed: the middleware refuses before the transport runs.
    from broker.main import app
    c = TestClient(app, follow_redirects=False)
    for path in ("/mcp", "/mcp/"):
        for headers in ({}, admin_headers, {"Authorization": "Bearer aab_nope"},
                        {"Authorization": "Basic x"}):
            r = c.post(path, json={}, headers=headers)
            assert r.status_code == 401, (path, headers)
            assert r.json()["code"] == "unauthorized"
            assert "aab_nope" not in r.text


def test_disabled_key_is_refused(live, echo_local, make_agent):
    from broker import db
    a = make_agent([cap(["list_items"])])
    with db.connect() as conn:
        conn.execute("UPDATE api_keys SET disabled = 1 WHERE id = ?", (a.key_id,))
    r = live.post("/mcp", json={}, headers={**a.headers, **ACCEPT})
    assert r.status_code == 401


def test_without_the_lifespan_a_valid_key_gets_503(env, make_agent):
    from broker.main import app
    a = make_agent()
    r = TestClient(app).post("/mcp", json={}, headers={**a.headers, **ACCEPT})
    assert r.status_code == 503 and r.json()["code"] == "unavailable"


def test_non_mcp_paths_still_reach_the_api(live):
    assert live.get("/health").status_code == 200
    assert live.get("/mcpx").status_code == 404        # prefix match is on a path segment


# ------------------------------------------------------------ DNS rebinding

def test_dns_rebinding_host_check(live, echo_local, make_agent):
    a = make_agent()
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    r = live.post("/mcp", json=body, headers={**a.headers, **ACCEPT, "Host": "evil.example"})
    assert r.status_code == 421
    r = live.post("/mcp", json=body, headers={**a.headers, **ACCEPT,
                                              "Origin": "http://evil.example"})
    assert r.status_code == 403
    r = live.post("/mcp", json=body, headers={**a.headers, **ACCEPT,
                                              "Host": "localhost:8080"})
    assert r.status_code == 200


def test_allowed_hosts_come_from_settings(env, monkeypatch):
    from broker.config import get_settings
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", " broker.example , localhost:* ,")
    get_settings.cache_clear()
    s = mcp_server.transport_security()
    assert s.enable_dns_rebinding_protection is True
    assert s.allowed_hosts == ["broker.example", "localhost:*"]


# ------------------------------------------------------------ live handshake

def test_initialize_handshake(live, echo_local, make_agent):
    a = make_agent()
    body = rpc(live, a.headers, "initialize", INITIALIZE)
    info = body["result"]["serverInfo"]
    assert info["name"] == "agent-authority-broker"
    from broker import __version__
    assert info["version"] == __version__
    assert "tools" in body["result"]["capabilities"]


def test_lifespan_can_run_twice(env, make_agent):
    # Each lifespan gets a fresh session manager (run() is once per instance).
    from broker.main import app
    a = make_agent()
    for _ in range(2):
        with TestClient(app) as c:
            assert set(tools(c, a.headers)) == GENERIC


# ------------------------------------------------------------ off the event loop

def _off_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return True
    return False


def test_blocking_work_runs_off_the_event_loop(live, echo_local, make_agent, monkeypatch):
    seen = []
    real_dispatch, real_tools, real_auth = (mcp_tools.dispatch, mcp_tools.tools_for,
                                            mcp_server.authenticate)

    def spy(fn, tag):
        def wrapped(*a, **kw):
            seen.append((tag, _off_loop()))
            return fn(*a, **kw)
        return wrapped

    monkeypatch.setattr(mcp_tools, "dispatch", spy(real_dispatch, "call"))
    monkeypatch.setattr(mcp_tools, "tools_for", spy(real_tools, "list"))
    monkeypatch.setattr(mcp_server, "authenticate", spy(real_auth, "auth"))
    a = make_agent([cap(["list_items"])])
    tools(live, a.headers)
    call(live, a.headers, "echo_list_items", {})
    assert {t for t, _ in seen} == {"auth", "list", "call"}
    assert all(off for _, off in seen), seen


def test_contextvar_auth_reaches_the_tools(echo_local, make_agent):
    a = make_agent([cap(["list_items"])])
    token = mcp_server.CURRENT_AUTH.set(a.auth)
    try:
        result = asyncio.run(mcp_server.call_tool("echo_list_items", {"room": "r1"}))
    finally:
        mcp_server.CURRENT_AUTH.reset(token)
    assert not result.isError
    assert [i["id"] for i in json.loads(result.content[0].text)["items"]] == ["i1", "i3"]


def test_no_auth_context_fails_closed(echo_local):
    result = asyncio.run(mcp_server.call_tool("echo_list_items", {}))
    assert result.isError and json.loads(result.content[0].text)["code"] == "unauthorized"
    with pytest.raises(PolicyError):
        asyncio.run(mcp_server.list_tools())


# ------------------------------------------------------------ results

def test_binary_result_is_an_embedded_resource(live, echo, make_agent):
    a = make_agent([cap(["get_blob"])])
    r = call(live, a.headers, "echo_get_blob", {"item_id": "i2"})
    assert r["isError"] is False
    [block] = r["content"]
    assert block["type"] == "resource"
    assert block["resource"]["mimeType"] == "application/x-echo"
    assert base64.b64decode(block["resource"]["blob"]) == b"blob:i2"


def test_image_binary_is_image_content():
    png = b"\x89PNG\r\n\x1a\nfake"
    out = mcp_tools._encode(engine.EngineResult(200, binary=png, mime="image/png"),
                            "echo", "get_blob")
    [block] = out.content
    assert block.type == "image" and block.mimeType == "image/png"
    assert base64.b64decode(block.data) == png


def test_refusals_are_compact_is_error_bodies(live, echo_local, make_agent):
    a = make_agent([cap(["post_item"], selector={"room": ["r1"]})])
    r = call(live, a.headers, "echo_post_item", {"room": "r2", "text": "x"})
    assert r["isError"] is True
    assert text_json(r) == {"error": "not covered by any of your grants",
                            "code": "out_of_grant", "hint": "request_permission"}
    r = call(live, a.headers, "no_such_tool_here", {})
    assert r["isError"] and text_json(r)["code"] == "not_found"
    r = call(live, a.headers, "nounderscore", {})
    assert r["isError"] and text_json(r)["code"] == "not_found"


def test_invalid_arguments_never_echo_input(live, echo_local, make_agent):
    a = make_agent([cap(["post_item"])])
    r = call(live, a.headers, "get_action_status", {"action_id": "x", "sneaky": "value-xyz"})
    assert r["isError"] and text_json(r)["code"] == "invalid_request"
    assert "value-xyz" not in json.dumps(r)
    r = call(live, a.headers, "echo_post_item", {"room": "r1", "text": "x",
                                                 "as_draft": "value-xyz"})
    assert r["isError"] and text_json(r)["code"] == "invalid_request"
    assert "value-xyz" not in json.dumps(r)
    r = call(live, a.headers, "echo_post_item", {"room": "r1", "text": "x",
                                                 "delay_seconds": True})
    assert text_json(r)["code"] == "invalid_request"


def test_internal_errors_hide_details(live, echo_local, make_agent, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("secret-internal-detail")
    monkeypatch.setattr(engine, "perform", boom)
    a = make_agent([cap(["list_items"])])
    r = call(live, a.headers, "echo_list_items", {})
    assert r["isError"] and text_json(r) == {"error": "internal error", "code": "internal"}
    assert "secret-internal-detail" not in json.dumps(r) and "Traceback" not in json.dumps(r)


def test_controls_queue_and_schedule(live, echo_local, make_agent):
    a = make_agent([cap(["post_item", "list_items"])])
    r = text_json(call(live, a.headers, "echo_post_item",
                       {"room": "r1", "text": "x", "as_draft": True, "note": "why"}))
    assert r["status"] == "pending_approval"
    status = text_json(call(live, a.headers, "get_action_status",
                            {"action_id": r["action_id"]}))
    assert status["status"] == "pending"
    r = text_json(call(live, a.headers, "echo_post_item",
                       {"room": "r1", "text": "x", "delay_seconds": 300}))
    assert r["status"] == "scheduled"
    # A control the action does not take is refused precisely, as over REST.
    r = call(live, a.headers, "echo_list_items", {"as_draft": True})
    assert r["isError"] and text_json(r)["code"] == "draft_unsupported"
    r = call(live, a.headers, "echo_list_items", {"delay_seconds": 300})
    assert r["isError"] and text_json(r)["code"] == "not_schedulable"


def test_generic_tools_work(live, echo_local, make_agent):
    a = make_agent([cap(["post_item"])])
    queued = text_json(call(live, a.headers, "echo_post_item",
                            {"room": "r1", "text": "x", "as_draft": True}))
    mine = text_json(call(live, a.headers, "list_my_actions", {"status": "pending"}))
    assert [i["id"] for i in mine["items"]] == [queued["action_id"]]
    assert text_json(call(live, a.headers, "cancel_action",
                          {"action_id": queued["action_id"]}))["status"] == "canceled"
    req = text_json(call(live, a.headers, "request_permission",
                         {"capabilities": [cap(["list_items"])], "reason": "need reads"}))
    assert req["status"] == "pending"
    got = text_json(call(live, a.headers, "get_permission_status", {"grant_id": req["id"]}))
    assert got["status"] == "pending"
    ids = [g["id"] for g in text_json(call(live, a.headers, "list_my_permissions"))["items"]]
    assert req["id"] in ids and a.grant_id in ids
    other = make_agent()
    r = call(live, other.headers, "get_permission_status", {"grant_id": req["id"]})
    assert r["isError"] and text_json(r)["code"] == "not_found"
