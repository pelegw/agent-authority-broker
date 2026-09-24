"""The runtime's log lines: every request runs under the broker's
X-Request-Id (or a fresh one) and gets one access line naming the service
credential that called; each /perform logs action, status and duration;
configure and secret-store writes name fields, never values; unexpected
failures keep the id. The logging modules are the broker's, byte for byte."""

import logging
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aab_plugin_runtime import SecretStore, from_env, serve
from aab_plugin_runtime import app as app_mod
from aab_plugin_runtime import logging_setup

from .conftest import TOKEN, echo_mod

REPO = Path(__file__).resolve().parents[2]
HEX32 = re.compile(r"[0-9a-f]{32}")


@pytest.mark.parametrize("name", ["logging_setup.py", "request_log.py"])
def test_the_logging_modules_are_the_brokers_byte_for_byte(name):
    assert (REPO / "plugin-runtime" / "aab_plugin_runtime" / name).read_bytes() == \
        (REPO / "broker" / "broker" / name).read_bytes()


@pytest.fixture()
def logs(caplog):
    caplog.set_level(logging.DEBUG)
    return caplog


def messages(caplog, logger):
    return [r.getMessage() for r in caplog.records if r.name == logger]


def test_perform_logs_action_status_and_duration_under_the_brokers_id(client, logs, capsys):
    capsys.readouterr()
    r = client.post("/perform", headers={"X-Request-Id": "broker-rid-1"},
                    json={"action": "list_items", "params": {"room": "r1"}})
    assert r.status_code == 200 and r.headers["x-request-id"] == "broker-rid-1"
    [line] = messages(logs, "aab_plugin_runtime")
    assert re.fullmatch(r"perform plugin=echo action=list_items status=200 duration_ms=\d+",
                        line)
    [access] = messages(logs, "aab_plugin_runtime.access")
    assert re.fullmatch(r"request method=POST path=/perform status=200 duration_ms=\d+ "
                        r"actor=plugin:echo ip=testclient", access)
    out = capsys.readouterr().out
    assert "[plugin-test broker-rid-1] perform plugin=echo" in out
    assert "r1" not in line and "room" not in out


def test_a_call_without_an_id_gets_a_fresh_one(client):
    r = client.get("/manifests")
    assert HEX32.fullmatch(r.headers["x-request-id"])


def test_a_failed_perform_logs_its_status_at_warning(client, echo, logs):
    echo.fail_next = 503
    assert client.post("/perform", json={"action": "list_items",
                                         "params": {"room": "r1"}}).status_code == 503
    [record] = [r for r in logs.records if r.name == "aab_plugin_runtime"]
    assert record.levelno == logging.WARNING and "status=503" in record.getMessage()


def test_an_unexpected_failure_is_a_502_that_keeps_the_request_id(client, echo, monkeypatch,
                                                                 logs, capsys):
    def boom(*_):
        raise RuntimeError("secret-bearing internals")
    monkeypatch.setattr(echo, "perform", boom)
    capsys.readouterr()
    r = client.post("/perform", headers={"X-Request-Id": "rid-boom"},
                    json={"action": "list_items", "params": {}})
    assert r.status_code == 502 and r.json() == {"error": "internal plugin error"}
    assert r.headers["x-request-id"] == "rid-boom"
    out = capsys.readouterr().out
    assert "[plugin-test rid-boom] unexpected adapter failure error=RuntimeError" in out
    assert "status=502" in messages(logs, "aab_plugin_runtime.access")[0]
    assert "secret-bearing" not in logs.text and "secret-bearing" not in out


def test_a_bad_token_is_refused_logged_and_has_no_actor(app, logs):
    c = TestClient(app, headers={"X-Plugin-Token": "not-the-token-0123456789"})
    assert c.get("/manifests").status_code == 401
    [refusal] = messages(logs, "aab_plugin_runtime")
    assert refusal == ("plugin API call refused: bad or missing X-Plugin-Token "
                       "path=/manifests ip=testclient header_present=true")
    assert "status=401" in messages(logs, "aab_plugin_runtime.access")[0]
    assert "actor=-" in messages(logs, "aab_plugin_runtime.access")[0]
    assert "not-the-token" not in logs.text and TOKEN not in logs.text


def test_configure_logs_field_names_never_values(client, logs):
    r = client.post("/configure", json={"config": {"greeting": "bonjour"},
                                        "secrets": {"api_secret": "configure-secret-value"}})
    assert r.status_code == 200
    assert messages(logs, "aab_plugin_runtime") == [
        "plugin configured plugin=echo config_fields=greeting secret_fields=api_secret"]
    assert messages(logs, "aab_plugin_runtime.secret_store") == [
        "secret slot written slot=echo stored=api_secret removed=-"]
    assert "configure-secret-value" not in logs.text and "bonjour" not in logs.text


def test_a_normalize_refusal_logs_the_kind_not_the_value(client, logs):
    r = client.post("/normalize", json={"kind": "room", "value": "private-value-xyz"})
    assert r.status_code == 400
    assert messages(logs, "aab_plugin_runtime") == [
        "normalize refused plugin=echo kind=room status=400"]
    assert "private-value-xyz" not in "\n".join(messages(logs, "aab_plugin_runtime"))


def test_secret_slot_wipes_are_logged_by_slot(tmp_path, key, logs):
    store = SecretStore(tmp_path, key)
    store.write("echo", {"a": "value-one", "b": None})     # b was never set: not news
    store.write("echo", {"a": None, "b": None})
    store.write("echo", {"b": ""})                          # a no-op write: no line
    store.wipe("echo")
    assert messages(logs, "aab_plugin_runtime.secret_store") == [
        "secret slot written slot=echo stored=a removed=-",
        "secret slot written slot=echo stored=- removed=a", "secret slot wiped slot=echo"]
    assert "value-one" not in logs.text


def test_the_service_name_is_given_or_derived_from_the_hosted_ids():
    echo = echo_mod.EchoAdapter()
    assert app_mod._service_name([echo], "google") == "google"
    assert app_mod._service_name([echo], None) == "echo"


def test_from_env_passes_the_service_name_to_serve(tmp_path, key, monkeypatch):
    seen = {}

    def fake_serve(adapters, token, secrets_dir, secrets_key, *, service=None):
        seen["service"] = service
    monkeypatch.setattr("aab_plugin_runtime.env.serve", fake_serve)
    from_env([echo_mod.EchoAdapter()], {"PLUGIN_TOKEN": TOKEN}, service="whatsapp")
    assert seen == {"service": "whatsapp"}


def test_serve_configures_logging_only_once_per_process(tmp_path, key):
    # conftest configured this process as "plugin-test"; a second serve()
    # under another name leaves the handlers alone.
    serve([echo_mod.EchoAdapter()], TOKEN, tmp_path, key, service="other")
    assert logging_setup._state.service == "plugin-test"
    assert len([h for h in logging.getLogger().handlers
                if isinstance(h, logging_setup.ConsoleHandler)]) == 1
