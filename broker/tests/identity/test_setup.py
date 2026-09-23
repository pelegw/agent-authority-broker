"""One-time owner setup: SETUP_TOKEN authorizes exactly one owner, then goes inert."""

import json

import pytest

from broker import db
from broker.identity import principals

from .conftest import OWNER_PASSWORD, SETUP_TOKEN


def _setup(client, token=SETUP_TOKEN, username="owner", password=OWNER_PASSWORD, **kw):
    body = {"username": username, "password": password}
    if token is not None:
        body["setup_token"] = token
    return client.post("/auth/setup", json=body, **kw)


def test_status_before_and_after_setup(client, setup_token):
    assert client.get("/auth/status").json() == {"setup_completed": False,
                                                 "login_required": False}
    assert _setup(client).status_code == 200
    assert client.get("/auth/status").json() == {"setup_completed": True,
                                                 "login_required": True}


def test_setup_requires_the_token(client, setup_token):
    missing = _setup(client, token=None)
    assert missing.status_code == 401
    assert missing.json() == {"error": "setup token required", "code": "unauthorized"}
    assert _setup(client, token="").status_code == 401
    wrong = _setup(client, token="not-the-token")
    assert wrong.status_code == 403
    assert wrong.json()["code"] == "forbidden"
    # Non-ASCII input is a clean refusal, never a compare_digest TypeError.
    assert _setup(client, token="töken☃").status_code == 403
    assert not principals.any_exists()


def test_setup_succeeds_once_and_is_audited(client, setup_token):
    r = _setup(client)
    assert r.status_code == 200
    assert r.json() == {"username": "owner", "setup_completed": True}
    assert db.get_config("setup_completed") == "1"
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM audit_log WHERE action = 'auth.setup'").fetchone()
        owner = conn.execute("SELECT * FROM principals").fetchone()
    assert row["actor"] == "owner"
    assert row["actor_principal"] == owner["id"]
    assert row["actor_via"] == "setup"
    # Neither the setup token nor the password lands anywhere in the audit log.
    with db.connect() as conn:
        dump = json.dumps([dict(r) for r in conn.execute("SELECT * FROM audit_log")])
    assert SETUP_TOKEN not in dump and OWNER_PASSWORD not in dump
    # The password is stored as a salted scrypt hash, never in the clear.
    assert owner["password_hash"] != OWNER_PASSWORD
    assert len(owner["password_hash"]) == 128 and len(owner["password_salt"]) == 32


def test_second_setup_is_409(client, setup_token):
    assert _setup(client).status_code == 200
    again = _setup(client, username="intruder")
    assert again.status_code == 409
    assert again.json()["code"] == "setup_completed"


def test_token_is_inert_after_setup_even_if_left_in_env(client, setup_token):
    assert _setup(client).status_code == 200
    # SETUP_TOKEN is still configured and correct, yet it can no longer act.
    for username in ("owner", "second"):
        assert _setup(client, username=username).status_code == 409
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM principals").fetchone()[0] == 1


def test_either_completion_signal_closes_setup(client, setup_token):
    # The app_config flag alone (e.g. principal row lost) keeps setup closed...
    db.set_config("setup_completed", "1")
    assert _setup(client).status_code == 409


def test_existing_principal_alone_closes_setup(client, setup_token):
    # ...and so does an existing principal without the flag (crash mid-setup).
    principals.create_owner("someone", OWNER_PASSWORD)
    assert _setup(client).status_code == 409


def test_weak_password_and_bad_username_are_400(client, setup_token):
    weak = _setup(client, password="short")
    assert weak.status_code == 400
    assert weak.json()["code"] == "weak_password"
    assert _setup(client, password="x" * 11).status_code == 400
    for bad in ("ab", "Owner", "has space", "x" * 33, "émile"):
        r = _setup(client, username=bad)
        assert r.status_code == 400 and r.json()["code"] == "invalid_username", bad
    assert not principals.any_exists()   # nothing was created along the way
    assert _setup(client, password="x" * 12).status_code == 200   # exactly 12 is fine


def test_setup_impossible_without_a_configured_token(client):
    # settings.setup_token is None: no value, including empty, can authorize setup.
    for token in (None, "", "anything"):
        r = _setup(client, token=token)
        assert r.status_code == 403
        assert r.json()["code"] == "setup_disabled"
    assert not principals.any_exists()


def test_wrong_setup_tokens_are_rate_limited(client, setup_token):
    for _ in range(5):
        assert _setup(client, token="guess").status_code == 403
    # Even the right token is refused while the IP is throttled.
    assert _setup(client).status_code == 429


def test_create_owner_refuses_a_second_principal(env):
    principals.create_owner("first", OWNER_PASSWORD)
    with pytest.raises(principals.OwnerExists):
        principals.create_owner("second", OWNER_PASSWORD)
