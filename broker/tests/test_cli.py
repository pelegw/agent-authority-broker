"""The `aab` CLI's phase 3 commands against the real app (TestClient stands in
for httpx.Client)."""

import json

import pytest
from fastapi.testclient import TestClient

from broker import engine, hidden
from cli import aab

from .conftest import cap


@pytest.fixture()
def run(env, monkeypatch, admin_token, capsys, logs_to_stderr):
    from broker.main import app
    monkeypatch.setattr(aab.httpx, "Client",
                        lambda base_url, headers, timeout: TestClient(app, base_url=base_url,
                                                                      headers=headers))

    def go(*argv):
        code = aab.main(["--token", admin_token, *argv])
        out = capsys.readouterr()
        return code, (json.loads(out.out) if out.out.strip() else None), out.err
    return go


def test_keys_create_list_rotate_disable(run, echo_local):
    caps = json.dumps([cap(["list_items"])])
    code, created, _ = run("keys", "create", "--name", "cli-bot", "--role", "full",
                           "--capabilities", caps, "--denies", '{"echo": {"room": ["r2"]}}')
    assert code == 0 and created["key"].startswith("aab_") and created["grant_id"]
    code, listed, _ = run("keys", "list")
    assert [k["name"] for k in listed] == ["cli-bot"]
    assert listed[0]["denies"] == {"echo": {"room": ["r2"]}}
    code, rotated, _ = run("keys", "rotate", str(created["id"]))
    assert rotated["key"] != created["key"]
    code, disabled, _ = run("keys", "disable", str(created["id"]))
    assert disabled["disabled"] is True


def test_keys_create_without_role_gets_the_brokers_default(run, echo_local, monkeypatch):
    """No --role: the CLI sends none, so the broker's owner default (full)
    applies; --role still sets a lower ceiling."""
    sent = []
    make_client = aab.httpx.Client          # the fixture's TestClient factory

    def spy(base_url, headers, timeout):
        c = make_client(base_url=base_url, headers=headers, timeout=timeout)
        original = c.post

        def post(url, **kw):
            sent.append(kw.get("json"))
            return original(url, **kw)
        c.post = post
        return c
    monkeypatch.setattr(aab.httpx, "Client", spy)
    code, created, _ = run("keys", "create", "--name", "plain")
    assert code == 0 and created["role"] == "full"
    assert "role" not in sent[-1]
    code, capped, _ = run("keys", "create", "--name", "capped", "--role", "read-draft")
    assert code == 0 and capped["role"] == "read-draft" and sent[-1]["role"] == "read-draft"


def test_bad_json_argument_is_a_usage_error(run, echo_local):
    with pytest.raises(SystemExit):
        run("keys", "create", "--name", "x", "--capabilities", "not json")


def test_grants_list_and_decide(run, echo_local, make_agent):
    a = make_agent([cap(["list_items"])])
    from broker.services import agent
    gid = agent.request_permission(a.auth, [cap(["post_item"])])["id"]
    code, pending, _ = run("grants", "list", "--status", "pending")
    assert [g["id"] for g in pending] == [gid]
    assert run("grants", "approve", gid)[1]["status"] == "active"
    assert run("grants", "revoke", gid)[1]["status"] == "revoked"
    code, _, err = run("grants", "reject", gid)
    assert code == 1 and "409" in err


def test_actions_list_approve_reject_cancel(run, echo_local, make_agent):
    d = make_agent([cap(["post_item"], mode="draft")])
    s = make_agent([cap(["post_item"])])
    ids = [engine.perform(d.auth, "echo", "post_item", {"room": "r1", "text": t}).body[
        "action_id"] for t in ("a", "b")]
    sid = engine.perform(s.auth, "echo", "post_item", {"room": "r1", "text": "c"},
                         delay_seconds=300).body["action_id"]
    code, listed, _ = run("actions", "list", "--status", "pending")
    assert sorted(a["id"] for a in listed["items"]) == sorted(ids)
    assert run("actions", "approve", ids[0])[1]["status"] == "done"
    assert run("actions", "reject", ids[1])[1]["status"] == "rejected"
    assert run("actions", "cancel", sid)[1]["status"] == "canceled"


def test_plugins_commands(run, vendored_echo, owner, echo_impl, monkeypatch):
    from .conftest import register_inprocess
    register_inprocess(echo_impl)
    code, listed, _ = run("plugins", "list")
    assert listed["items"][0]["id"] == "echo"
    monkeypatch.setattr(aab.getpass, "getpass", lambda prompt: "cli-secret-value")
    code, cfg, _ = run("plugins", "config", "echo", "--set", "greeting=hey",
                       "--secret", "api_secret")
    assert code == 0 and cfg["config"]["greeting"] == "hey"
    assert "cli-secret-value" not in json.dumps(cfg)
    assert run("plugins", "enable", "echo")[1]["enabled"] is True
    assert run("plugins", "health", "echo")[1]["last_health"]["api_secret_set"] is True
    assert run("plugins", "disable", "echo")[1]["enabled"] is False


def test_hidden_commands(run, echo_local):
    code, added, _ = run("hidden", "add", "echo", "room", "R2", "--reason", "private")
    assert added["resource_id"] == "r2"
    assert [h["resource_id"] for h in run("hidden", "list", "--target", "echo")[1]] == ["r2"]
    assert run("hidden", "rm", "echo", "room", "r2")[1]["removed"] is True
    assert hidden.list_hidden() == []


def test_decisions_commands(run, echo_local, make_agent):
    a = make_agent([cap(["list_items"])])
    engine.perform(a.auth, "echo", "list_items", {})
    code, listed, _ = run("decisions", "list", "--decision", "allow", "--limit", "5")
    assert [d["decision"] for d in listed["items"]] == ["allow"]
    assert run("decisions", "verify")[1]["ok"] is True
