"""One id per request, everywhere: the broker's log lines, the plugin
runtime's log lines for the calls that request made, and the decision rows it
recorded all carry the same request id. Background work (the scheduler, a
Telegram tap) runs under ids of its own (`sched-`, `tg-`) that its decision
rows carry too.

The lines are read from the real handler's output (stdout), formatted exactly
as `docker compose logs` would show them."""

import re

import pytest

from broker import db, engine
from broker.actions import queue, scheduler

from .conftest import cap, enable_plugin, register_remote
from .mcp_helpers import ACCEPT, live  # noqa: F401  (live: fixture)

LINE = re.compile(r"^\S+Z (\w+) (\S+) \[(\S+) (\S+)\] (.*)$")


def parsed(out: str) -> list[tuple[str, str, str, str]]:
    """(level, logger, request_id, message) for every formatted line."""
    return [(m[1], m[2], m[4], m[5]) for m in map(LINE.match, out.splitlines()) if m]


def decision_rows() -> list[tuple[str, str]]:
    with db.connect() as conn:
        return [(r["kind"], r["request_id"]) for r in conn.execute(
            "SELECT kind, request_id FROM decisions ORDER BY id")]


@pytest.fixture()
def remote_echo(env, owner, echo_impl, tmp_path, vendored_echo):
    """The echo plugin behind the real plugin runtime (RemoteAdapter over a
    TestClient bound to serve()), enabled and connected."""
    register_remote(echo_impl, tmp_path)
    enable_plugin()
    return echo_impl


def test_one_rest_call_has_one_id_in_broker_runtime_and_decision_record(
        client, remote_echo, make_agent, capsys):
    agent = make_agent([cap(["list_items"])], name="corr-agent")
    capsys.readouterr()
    r = client.post("/v1/targets/echo/actions/list_items", json={"params": {"room": "r1"}},
                    headers=agent.headers)
    assert r.status_code == 200, r.text
    rid = r.headers["x-request-id"]
    lines = parsed(capsys.readouterr().out)
    by_logger = {}
    for level, logger, line_rid, message in lines:
        by_logger.setdefault(logger, []).append((line_rid, message))

    def ids(logger, prefix):
        return {i for i, m in by_logger.get(logger, []) if m.startswith(prefix)}

    assert ids("broker.access", "request method=POST path=/v1/targets/echo") == {rid}
    assert ids("broker.engine", "decision decision=allow") == {rid}
    assert ids("broker.engine", "outcome outcome=ok status=200") == {rid}
    # The plugin runtime's own lines for the call the broker made.
    assert ids("aab_plugin_runtime.access", "request method=POST path=/perform") == {rid}
    assert ids("aab_plugin_runtime", "perform plugin=echo action=list_items status=200") == \
        {rid}
    assert decision_rows() == [("decision", rid), ("outcome", rid)]
    # Every plugin call the request made carries it (the selector's
    # normalize too), and names the credential that called.
    runtime_access = [m for i, m in by_logger["aab_plugin_runtime.access"] if i == rid]
    assert {m.split()[2] for m in runtime_access} == {"path=/normalize", "path=/perform"}
    assert all("actor=plugin:echo" in m for m in runtime_access)


def test_an_inbound_id_reaches_the_plugin_and_the_record(client, remote_echo, make_agent,
                                                         capsys):
    agent = make_agent([cap(["list_items"])])
    r = client.post("/v1/targets/echo/actions/list_items", json={"params": {"room": "r1"}},
                    headers={**agent.headers, "X-Request-Id": "agent-run-42"})
    assert r.headers["x-request-id"] == "agent-run-42"
    runtime = [x for x in parsed(capsys.readouterr().out)
               if x[1].startswith("aab_plugin_runtime")]
    assert runtime and {x[2] for x in runtime} == {"agent-run-42"}
    assert decision_rows() == [("decision", "agent-run-42"), ("outcome", "agent-run-42")]


def test_an_mcp_tool_call_records_the_http_request_id(live, remote_echo, make_agent):
    """The MCP server runs the tool in a task spawned per request: it must
    inherit the request's context, as it does the caller's key."""
    agent = make_agent([cap(["list_items"])])
    r = live.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                "params": {"name": "echo_list_items",
                                           "arguments": {"room": "r1"}}},
                  headers={**agent.headers, **ACCEPT, "X-Request-Id": "mcp-call-1"})
    assert r.status_code == 200, r.text
    assert r.json()["result"].get("isError") is not True, r.text
    assert r.headers["x-request-id"] == "mcp-call-1"
    assert decision_rows() == [("decision", "mcp-call-1"), ("outcome", "mcp-call-1")]


def test_each_scheduled_delivery_runs_under_its_own_sched_id(echo_local, make_agent, capsys):
    agent = make_agent([cap(["post_item"])])
    ids = []
    for text in ("one", "two"):
        r = engine.perform(agent.auth, "echo", "post_item", {"room": "r1", "text": text},
                           delay_seconds=120)
        ids.append(r.body["action_id"])
    with db.connect() as conn:
        conn.execute("UPDATE actions SET run_at = 1")
    capsys.readouterr()
    assert scheduler._tick() == 2
    delivered = [rid for kind, rid in decision_rows()[2:]]          # after the two queuings
    assert len(delivered) == 4 and all(re.fullmatch(r"sched-[0-9a-f]{32}", i) for i in delivered)
    first, second = delivered[:2], delivered[2:]
    assert len(set(first)) == 1 and len(set(second)) == 1 and set(first) != set(second)
    lines = parsed(capsys.readouterr().out)
    tick = [x for x in lines if x[3].startswith("scheduler tick")]
    assert len(tick) == 1 and "due=2 claimed=2 not_delivered=0" in tick[0][3]
    delivered_lines = {x[2] for x in lines if x[3].startswith("action delivered")}
    assert delivered_lines == set(first) | set(second)
    assert all(queue.get_row(i)["status"] == "done" for i in ids)


def test_an_idle_tick_logs_nothing(echo_local, capsys):
    capsys.readouterr()
    assert scheduler._tick() == 0
    assert not [x for x in parsed(capsys.readouterr().out)
                if x[1].startswith("broker.actions")]


def test_a_telegram_tap_runs_under_a_tg_id(echo_local, make_agent, fake_telegram, capsys):
    from broker.notify import telegram_inbound as inbound
    agent = make_agent([cap(["post_item"], mode="draft")])
    r = engine.perform(agent.auth, "echo", "post_item", {"room": "r1", "text": "hi"})
    action_id = r.body["action_id"]
    capsys.readouterr()
    inbound._handle_update({"update_id": 1, "callback_query": {
        "id": "cb1", "data": f"a:approve:{action_id}", "from": {"id": 4242},
        "message": {"message_id": 10, "chat": {"id": 4242}}}})
    assert queue.get_row(action_id)["status"] == "done"
    tap_ids = {rid for kind, rid in decision_rows()[1:]}
    [tap_id] = tap_ids
    assert re.fullmatch(r"tg-[0-9a-f]{32}", tap_id)
    lines = parsed(capsys.readouterr().out)
    accepted = [x for x in lines if x[3].startswith("telegram tap accepted")]
    assert accepted and accepted[0][2] == tap_id
    assert "result=done by=owner" in accepted[0][3]
    approved = [x for x in lines if x[3].startswith("action approved")]
    assert approved and approved[0][2] == tap_id and "via=telegram" in approved[0][3]
