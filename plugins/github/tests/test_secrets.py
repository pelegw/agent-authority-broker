"""Secrets and minted tokens never leave the plugin: not in a response, a
log line, a repr, or anything written to disk."""

import logging

import pytest

from aab_plugin_github.call import Call
from aab_plugin_github.tokens import GitHubToken

from .conftest import APP_CONFIG, configure, install
from .fakes import PAT, PRIVATE_KEY_PEM
from .test_minting import CALLS

PEM_BODY = PRIVATE_KEY_PEM.splitlines()[1]          # a line of key material


def minted(gh) -> list[str]:
    return list(gh.tokens)


@pytest.fixture()
def exercised(client, gh, caplog, perform):
    """Configure both secrets, install, and drive successes and failures."""
    caplog.set_level(logging.DEBUG)
    configure(client, APP_CONFIG, {"private_key_pem": PRIVATE_KEY_PEM, "pat": PAT})
    install(client)
    for action in ("get_file", "create_issue", "push_file"):
        perform(action, CALLS[action])
    gh.fail("POST", r"/repos/octo/a/issues$", "read_timeout")
    perform("create_issue", CALLS["create_issue"])
    gh.widen_tokens = True
    perform("list_issues", {"repo": "octo/b"})                # refused wider token
    gh.widen_tokens = False
    gh.tokens.clear()
    perform("get_file", CALLS["get_file"])                    # refused (401) token
    client.post("/configure", json={"config": {}, "secrets": {"private_key_pem": "bad"}})
    return client


def test_no_secret_or_token_in_any_log_line(exercised, gh, caplog):
    # Everything was driven in the fixture, so read the setup phase too.
    records = caplog.get_records("setup") + caplog.get_records("call")
    text = "\n".join(f"{r.name} {r.getMessage()} {r.exc_text or ''}" for r in records)
    # Not vacuous: the refused wider token was logged (by unit names only).
    assert "refusing an installation token wider than requested" in text
    assert PEM_BODY not in text and PAT not in text
    for token in minted(gh):
        assert token not in text


def test_no_secret_or_token_in_any_response(exercised, gh):
    bodies = [exercised.get("/status").text, exercised.get("/manifests").text,
              exercised.post("/connect/start", json={}).text]
    for body in bodies:
        assert PEM_BODY not in body and PAT not in body
        assert not any(t in body for t in minted(gh))


def test_no_token_is_ever_written_to_the_secret_store(exercised, gh, app, tmp_path):
    store = app.state.secret_store
    stored = str(store.read_all("github")) + str(store.read_all("github_app"))
    assert not any(t in stored for t in minted(gh))
    for path in (tmp_path / "secrets").iterdir():
        raw = path.read_bytes()
        assert PAT.encode() not in raw and PEM_BODY.encode() not in raw   # encrypted at rest
        assert not any(t.encode() in raw for t in minted(gh))


def test_reprs_redact(exercised, adapter, gh):
    token = adapter.connection.mint({"permissions": {"contents": "read"},
                                     "resources": {"repo": ["octo/a"]}})
    call = Call(adapter.api, adapter.connection, "get_file", {}, "octo/a", token)
    for obj in (adapter, adapter.connection, adapter.api, token, call,
                adapter.connection._tokens, adapter._slot):
        text = repr(obj) + str(obj)
        assert token.bearer() not in text and PAT not in text and PEM_BODY not in text
    assert "<redacted>" in repr(token)


def test_a_token_value_is_reachable_only_through_bearer():
    t = GitHubToken("app", ("a",), (("contents", "read"),), "ghs_secretvalue")
    assert t.bearer() == "ghs_secretvalue"
    assert "ghs_secretvalue" not in repr(t) and "ghs_secretvalue" not in f"{t}"
    # Equality ignores the value, so a token never leaks through a comparison
    # message either.
    assert t == GitHubToken("app", ("a",), (("contents", "read"),), "other")


def test_errors_never_carry_the_key(client):
    r = client.post("/configure", json={"config": APP_CONFIG,
                                        "secrets": {"private_key_pem": PRIVATE_KEY_PEM[:200]}})
    assert r.status_code == 400
    assert PRIVATE_KEY_PEM[40:120] not in r.text
