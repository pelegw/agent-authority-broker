"""The broker's deliberate log lines at its operational seams: each says what
happened in a fixed message with key=value fields, and none carries params,
message text, labels, notes, secrets or tokens."""

import logging

import pytest

from broker import db, decisions, engine
from broker.errors import PolicyError
from broker.identity import ratelimit
from broker.plugins import settings
from broker.plugins.registry import get_registry

from .conftest import CSRF_HEADERS, OWNER_PASSWORD, cap


@pytest.fixture()
def logs(caplog):
    caplog.set_level(logging.DEBUG)

    def messages(logger: str | None = None, starts: str = "") -> list[str]:
        return [r.getMessage() for r in caplog.records
                if (logger is None or r.name == logger) and r.getMessage().startswith(starts)]
    messages.text = lambda: caplog.text
    messages.records = lambda: caplog.records
    return messages


def test_the_boot_line_names_secrets_as_set_or_unset_and_survives_redaction(env, monkeypatch,
                                                                            logs):
    from broker import main
    from broker.config import get_settings
    from broker.logging_setup import redact
    monkeypatch.setenv("SETUP_TOKEN", "boot-line-setup-token-value")
    monkeypatch.setenv("DECISION_SIGNING_KEY", "ab" * 32)
    get_settings.cache_clear()
    main.log_boot("wal")
    [line] = logs("broker.main", "broker starting")
    assert "journal_mode=wal" in line and "public_mode=false" in line
    assert "secrets_set=decision_signing_key,setup_token " in line
    assert "secrets_unset=broker_secrets_key,origin_secret " in line
    assert redact(line) == line                   # nothing in it looks like a secret
    assert "boot-line-setup-token-value" not in logs.text() and "ab" * 32 not in logs.text()
    main.log_boot("delete")
    assert logs("broker.main", "database is not in WAL mode")


def test_decision_and_outcome_lines(echo_local, make_agent, logs):
    agent = make_agent([cap(["post_item"])], name="writer")
    engine.perform(agent.auth, "echo", "post_item", {"room": "r1", "text": "private words"})
    [decision] = logs("broker.engine", "decision ")
    assert decision.startswith("decision decision=allow reason=covered status=200 target=echo "
                               "action=post_item key=writer resource=r1 chain=1 row=1")
    [outcome] = logs("broker.engine", "outcome ")
    assert outcome.startswith("outcome outcome=ok status=200 target=echo action=post_item "
                              "key=writer duration_ms=")
    assert "private words" not in logs.text()


def test_a_deny_is_one_decision_line_and_no_outcome(echo_local, make_agent, logs):
    agent = make_agent([cap(["list_items"])])
    with pytest.raises(PolicyError):
        engine.perform(agent.auth, "echo", "post_item", {"room": "r1", "text": "x"})
    [decision] = logs("broker.engine", "decision ")
    assert "decision=deny reason=out_of_grant status=403" in decision and "chain=0" in decision
    assert logs("broker.engine", "outcome ") == []


def test_an_exhausted_budget_names_the_grant(echo_local, make_agent, logs):
    agent = make_agent([cap(["post_item"], budget={"per_day": 1})], name="budgeted")
    engine.perform(agent.auth, "echo", "post_item", {"room": "r1", "text": "one"})
    with pytest.raises(PolicyError):
        engine.perform(agent.auth, "echo", "post_item", {"room": "r1", "text": "two"})
    [refused] = logs("broker.engine", "budget refused")
    assert refused.startswith(f"budget refused code=budget_exhausted grant={agent.grant_id} "
                              "budget=per_day key=budgeted")
    assert "outcome outcome=budget_exhausted status=429" in logs("broker.engine", "outcome ")[-1]
    assert [r.levelname for r in logs.records() if r.getMessage().startswith("budget")] == \
        ["WARNING"]


def test_a_failed_plugin_call_is_a_warning_outcome(echo_local, make_agent, logs):
    agent = make_agent([cap(["post_item"])])
    echo_local.impl.fail_next = 502
    with pytest.raises(PolicyError):
        engine.perform(agent.auth, "echo", "post_item", {"room": "r1", "text": "x"})
    [record] = [r for r in logs.records() if r.getMessage().startswith("outcome ")]
    assert record.levelno == logging.WARNING and "outcome=unknown status=502" in record.getMessage()


def test_owner_login_lines(client, owner, logs):
    ratelimit.reset()
    wrong = "an-incorrect-password-XYZ"
    client.post("/auth/login", json={"username": owner.username, "password": wrong})
    # A password typed into the username box is not logged as a username.
    client.post("/auth/login", json={"username": OWNER_PASSWORD, "password": wrong})
    client.post("/auth/login", json={"username": owner.username, "password": owner.password})
    assert logs("broker.routers.auth", "owner login failed") == [
        "owner login failed username=owner ip=testclient",
        "owner login failed username=(invalid) ip=testclient"]
    assert logs("broker.routers.auth", "owner logged in") == [
        "owner logged in username=owner ip=testclient"]
    for _ in range(5):
        client.post("/auth/login", json={"username": "owner", "password": wrong})
    assert logs("broker.identity.ratelimit", "owner credential attempts rate-limited")
    assert wrong not in logs.text() and OWNER_PASSWORD not in logs.text()
    ratelimit.reset()


def test_setup_lines(client, env, monkeypatch, logs):
    from broker.config import get_settings
    ratelimit.reset()
    monkeypatch.setenv("SETUP_TOKEN", "setup-token-for-log-lines-0123456789")
    get_settings.cache_clear()
    body = {"username": "owner", "password": OWNER_PASSWORD}
    assert client.post("/auth/setup", json={**body, "setup_token": "a-wrong-setup-token"}
                       ).status_code == 403
    assert client.post("/auth/setup", json={**body, "setup_token":
                                            "setup-token-for-log-lines-0123456789"}
                       ).status_code == 200
    assert logs(starts="owner setup refused: wrong setup token") and \
        logs(starts="owner setup completed")
    assert "setup-token-for-log-lines" not in logs.text() and "a-wrong-setup" not in logs.text()
    ratelimit.reset()


def test_admin_token_and_key_lines_name_ids_never_secrets(client, admin_headers, echo_local,
                                                          logs):
    r = client.post("/v1/admin/tokens", json={"name": "deploy"}, headers=admin_headers)
    token = r.json()
    r = client.post("/v1/admin/keys", headers=admin_headers, json={
        "name": "bot-1", "role": "full", "rate_per_min": 30,
        "capabilities": [cap(["list_items"])]})
    key = r.json()
    assert logs(starts="admin token created")[0].startswith(
        f"admin token created token_id={token['id']} name=deploy")
    assert logs(starts="key created")[0].startswith(f"key created key_id={key['id']} name=bot-1")
    client.post(f"/v1/admin/tokens/{token['id']}/revoke", headers=admin_headers)
    assert logs(starts="admin token revoked") == [
        f"admin token revoked token_id={token['id']} by=owner via=token"]
    assert token["token"] not in logs.text() and key["key"] not in logs.text()


def test_plugin_configure_logs_field_names_only(client, admin_headers, echo_local, logs):
    r = client.patch("/v1/admin/plugins/echo", headers=admin_headers,
                     json={"config": {"greeting": "hola", "api_secret": "s3cr3t-api-value"}})
    assert r.status_code == 200, r.text
    [line] = logs("broker.services.plugins_admin", "plugin configured")
    assert line == ("plugin configured plugin=echo fields=greeting secret_fields=api_secret "
                    "relayed=true by=owner via=token")
    assert "s3cr3t-api-value" not in logs.text() and "hola" not in logs.text()


def test_an_unreachable_plugin_service_is_a_warning_with_its_status(env, logs):
    import httpx

    def unreachable(base_url, headers, timeout):
        return httpx.Client(transport=httpx.MockTransport(
            lambda request: (_ for _ in ()).throw(httpx.ConnectError("refused"))))

    get_registry().discover({"ghost": ("http://ghost:8090", "t0ken-value-0123456789")},
                            client_factory=unreachable)
    [line] = logs("broker.plugins.registry", "plugin service not reachable")
    assert line == "plugin service not reachable yet; will retry service=ghost status=503 " \
                   "retry_seconds=30"
    assert "t0ken-value" not in logs.text()


def test_connection_changes_are_logged_once(echo_local, logs):
    settings.set_health("echo", {"connected": True, "healthy": True}, True)     # unchanged
    settings.set_health("echo", {"connected": False, "healthy": False}, False)
    settings.set_health("echo", {"connected": False, "healthy": False,
                                 "enforcement": "proxy"}, False)
    assert logs("broker.plugins.settings") == [
        "plugin connection changed plugin=echo connected=false healthy=false enforcement=-",
        "plugin enforcement changed plugin=echo enforcement=proxy previous=-"]


def test_a_broken_chain_verification_is_an_error_line(client, admin_headers, echo_local,
                                                      make_agent, logs):
    agent = make_agent([cap(["list_items"])])
    engine.perform(agent.auth, "echo", "list_items", {"room": "r1"})
    assert client.get("/v1/admin/decisions/verify", headers=admin_headers).json()["ok"]
    with db.connect() as conn:
        conn.execute("UPDATE decisions SET reason = 'tampered' WHERE id = 1")
    assert not decisions.verify()["ok"]
    client.get("/v1/admin/decisions/verify", headers=admin_headers)
    ok, bad = [r for r in logs.records() if r.getMessage().startswith("decision chain")]
    assert ok.levelno == logging.INFO and "ok=true" in ok.getMessage()
    assert bad.levelno == logging.ERROR and "ok=false checked=1 first_bad_id=1" in \
        bad.getMessage()


def test_action_lifecycle_lines(client, admin_headers, echo_local, make_agent, logs):
    agent = make_agent([cap(["post_item"], mode="draft")], name="drafter")
    r = client.post("/v1/targets/echo/actions/post_item", headers=agent.headers,
                    json={"params": {"room": "r1", "text": "draft body"},
                          "note": "a private note for the owner"})
    action_id = r.json()["action_id"]
    client.post(f"/v1/admin/actions/{action_id}/approve", headers=admin_headers)
    assert logs(starts="action created")[0].startswith(
        f"action created action_id={action_id} status=pending target=echo action=post_item "
        "key=drafter")
    assert logs(starts="action approved") == [
        f"action approved action_id={action_id} target=echo action=post_item by=owner via=token"]
    assert logs(starts="action delivered")[0].startswith(f"action delivered action_id={action_id}")
    assert "draft body" not in logs.text() and "private note" not in logs.text()


def test_delegation_lines(client, echo_local, make_agent, logs):
    parent = make_agent([cap(["list_items"])], name="lead")
    r = client.post("/v1/delegations", headers=parent.headers,
                    json={"name": "helper", "capabilities": [cap(["list_items"])]})
    assert r.status_code == 201, r.text
    child = r.json()
    client.post(f"/v1/delegations/{child['key_id']}/revoke", headers=parent.headers)
    assert logs(starts="delegation created")[0].startswith(
        f"delegation created key=lead child_key_id={child['key_id']} child=lead/helper")
    assert logs(starts="delegation revoked")[0].startswith(
        f"delegation revoked key=lead child_key_id={child['key_id']}")
    assert child["key"] not in logs.text()


def test_a_failed_notification_is_a_warning(echo_local, make_agent, monkeypatch, logs):
    from broker import notify

    class Broken:
        __name__ = "broken"

        def notify_action(self, item):
            raise RuntimeError("chat 4242 said no")

    monkeypatch.setattr(notify, "_PROVIDERS", [Broken()])
    agent = make_agent([cap(["post_item"], mode="draft")])
    engine.perform(agent.auth, "echo", "post_item", {"room": "r1", "text": "x"})
    [line] = logs("broker.notify", "notification failed")
    assert "provider=broken method=notify_action" in line and "error=RuntimeError" in line
    assert "4242" not in line
