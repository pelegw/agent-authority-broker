"""The broker's RequestContextMiddleware: a request id on every response
(the caller's when well formed and not a reserved background prefix), one
access line per request with method, path, status, duration, actor and ip,
and NEVER the query string (the OAuth callback's carries a code)."""

import asyncio
import logging
import re

import pytest

from broker import db
from broker.logging_setup import current_request_id
from broker.request_log import RequestContextMiddleware

from .conftest import cap

HEX32 = re.compile(r"[0-9a-f]{32}")


def access_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "broker.access"]


def test_every_response_carries_a_generated_request_id(client):
    r = client.get("/auth/status")
    assert HEX32.fullmatch(r.headers["x-request-id"])
    assert r.headers["x-request-id"] != client.get("/auth/status").headers["x-request-id"]


def test_a_well_formed_inbound_id_is_used_and_echoed(client):
    r = client.get("/auth/status", headers={"X-Request-Id": "agent-run-7.step_2"})
    assert r.headers["x-request-id"] == "agent-run-7.step_2"


@pytest.mark.parametrize("supplied", ["has space", "x" * 129, "quote\"d", "",
                                      "sched-" + "a" * 32, "tg-" + "b" * 32])
def test_a_malformed_or_reserved_inbound_id_is_replaced(client, supplied):
    """Reserved: the scheduler's and Telegram's prefixes, so no caller can
    make its calls look like the broker's own background work."""
    r = client.get("/auth/status", headers={"X-Request-Id": supplied})
    assert HEX32.fullmatch(r.headers["x-request-id"])


def test_the_access_line_never_carries_the_query_string(client, caplog):
    caplog.set_level(logging.DEBUG)
    r = client.get("/oauth/callback/google?code=4/0AQSTgQE-a-real-looking-code"
                   "&state=nonce-abc123&scope=gmail")
    assert r.status_code == 200
    [line] = access_lines(caplog)
    assert "method=GET path=/oauth/callback/google status=200" in line
    everything = caplog.text + "\n".join(r.getMessage() for r in caplog.records)
    for value in ("4/0AQSTgQE", "nonce-abc123", "code=", "state=", "scope="):
        assert value not in everything


def test_the_access_line_fields(client, caplog):
    caplog.set_level(logging.INFO)
    client.get("/auth/status", headers={"X-Request-Id": "rid-fields"})
    [record] = [r for r in caplog.records if r.name == "broker.access"]
    assert record.levelno == logging.INFO
    assert re.fullmatch(r"request method=GET path=/auth/status status=200 duration_ms=\d+ "
                        r"actor=- ip=testclient", record.getMessage())


def test_health_probes_are_not_logged(client, caplog):
    caplog.set_level(logging.INFO)
    assert client.get("/health").status_code == 200
    assert access_lines(caplog) == []


def test_the_owner_health_summary_is_not_logged_either(client, echo_local, admin_headers,
                                                       caplog):
    # A monitor asks every minute; like the liveness probe, it is noise.
    caplog.set_level(logging.INFO)
    assert client.get("/v1/admin/health", headers=admin_headers).status_code == 200
    assert access_lines(caplog) == []


def test_the_actor_is_the_agent_key(client, echo_local, make_agent, caplog):
    agent = make_agent([cap(["list_items"])], name="reader-7")
    caplog.set_level(logging.INFO)
    r = client.get("/v1/targets", headers=agent.headers)
    assert r.status_code == 200
    assert "actor=key:reader-7" in access_lines(caplog)[-1]


def test_the_actor_is_the_owner_on_the_admin_plane(client, admin_headers, caplog):
    caplog.set_level(logging.INFO)
    assert client.get("/v1/admin/keys", headers=admin_headers).status_code == 200
    assert "actor=owner:owner" in access_lines(caplog)[-1]


def test_a_refused_agent_call_logs_the_reason_class_and_no_actor(client, caplog):
    caplog.set_level(logging.INFO)
    bogus = "aab_" + "0" * 48
    r = client.get("/v1/targets", headers={"Authorization": f"Bearer {bogus}"})
    assert r.status_code == 401
    assert "status=401" in access_lines(caplog)[-1] and "actor=-" in access_lines(caplog)[-1]
    [refusal] = [r.getMessage() for r in caplog.records if r.name == "broker.agent_auth"]
    assert refusal == "agent authentication failed reason=unknown_key ip=testclient"
    assert bogus not in caplog.text


@pytest.mark.parametrize("header,reason", [
    (None, "missing"), ("Basic dXNlcjpwYXNz", "malformed"),
    ("Bearer aab_admin_" + "1" * 48, "admin_token"), ("Bearer nope", "malformed"),
])
def test_agent_refusal_reason_classes(client, caplog, header, reason):
    caplog.set_level(logging.INFO)
    headers = {"Authorization": header} if header else {}
    assert client.get("/v1/targets", headers=headers).status_code == 401
    assert f"reason={reason}" in caplog.text
    assert "1" * 48 not in caplog.text


def test_the_decision_row_records_the_request_id(client, echo_local, make_agent):
    agent = make_agent([cap(["list_items"])])
    r = client.post("/v1/targets/echo/actions/list_items", json={"params": {"room": "r1"}},
                    headers={**agent.headers, "X-Request-Id": "rid-decision-1"})
    assert r.status_code == 200, r.text
    assert r.headers["x-request-id"] == "rid-decision-1"
    with db.connect() as conn:
        rows = [(x["kind"], x["request_id"]) for x in conn.execute(
            "SELECT kind, request_id FROM decisions ORDER BY id")]
    assert rows == [("decision", "rid-decision-1"), ("outcome", "rid-decision-1")]


# ---- the middleware on its own ---------------------------------------------------------

def _run(app, path="/x", headers=()):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": path, "raw_path": path.encode(),
             "query_string": b"a=1", "headers": list(headers), "client": ("10.1.2.3", 5)}
    mw = RequestContextMiddleware(app, access_logger="broker.access")
    asyncio.run(mw(scope, receive, send))
    return sent


def test_a_failing_app_is_logged_as_500_at_warning_and_reraised(caplog):
    async def boom(scope, receive, send):
        raise RuntimeError("kaput")

    caplog.set_level(logging.INFO)
    with pytest.raises(RuntimeError):
        _run(boom)
    [record] = [r for r in caplog.records if r.name == "broker.access"]
    assert record.levelno == logging.WARNING and "status=500" in record.getMessage()
    assert "ip=10.1.2.3" in record.getMessage()


def test_the_app_runs_under_the_id_and_the_header_is_replaced_not_duplicated():
    seen = {}

    async def app(scope, receive, send):
        seen["rid"] = current_request_id()
        await send({"type": "http.response.start", "status": 204,
                    "headers": [(b"x-request-id", b"forged"), (b"content-type", b"x/y")]})
        await send({"type": "http.response.body", "body": b""})

    sent = _run(app, headers=[(b"x-request-id", b"inbound-1")])
    headers = sent[0]["headers"]
    assert seen["rid"] == "inbound-1"
    assert [v for k, v in headers if k == b"x-request-id"] == [b"inbound-1"]
    assert (b"content-type", b"x/y") in headers
    assert current_request_id() is None                  # reset after the request


def test_a_path_is_logged_percent_encoded_and_query_free(caplog):
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 404, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    caplog.set_level(logging.INFO)
    _run(app, path="/v1/x%0Ay?code=1")
    [line] = access_lines(caplog)
    assert "path=/v1/x%0Ay " in line and "code" not in line
