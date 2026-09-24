"""crypto.py, the broker-side secret store: Fernet under BROKER_SECRETS_KEY in
plugin_secrets, fail-closed boot, and "unreadable" (re-enter required) when
the key changes. No value or key ever appears in an error or a log."""

import logging

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from broker import crypto, db
from broker.config import get_settings

SLOT, NAME, VALUE = crypto.BROKER_SLOT, "telegram_bot_token", "123456:super-secret-value"


def _rows():
    with db.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM plugin_secrets")]


def _set_key(monkeypatch, key):
    if key is None:
        monkeypatch.delenv("BROKER_SECRETS_KEY", raising=False)
    else:
        monkeypatch.setenv("BROKER_SECRETS_KEY", key)
    get_settings.cache_clear()


def test_round_trip_and_ciphertext_only_at_rest(secrets_key):
    assert crypto.get(SLOT, NAME) is None and crypto.state(SLOT, NAME) == "unset"
    crypto.put(SLOT, NAME, VALUE)
    assert crypto.get(SLOT, NAME) == VALUE
    assert crypto.state(SLOT, NAME) == "set"
    [row] = _rows()
    assert (row["slot"], row["name"]) == (SLOT, NAME)
    assert VALUE.encode() not in bytes(row["ciphertext"])
    crypto.put(SLOT, NAME, "replaced-value")                 # upsert, one row
    assert crypto.get(SLOT, NAME) == "replaced-value" and len(_rows()) == 1


def test_put_requires_the_key(env):
    assert not crypto.key_configured()
    with pytest.raises(crypto.SecretsUnavailable):
        crypto.put(SLOT, NAME, VALUE)
    assert _rows() == []


def test_empty_values_are_refused(secrets_key):
    with pytest.raises(ValueError):
        crypto.put(SLOT, NAME, "")


def test_a_rotated_key_makes_the_secret_unreadable_not_a_crash(secrets_key, monkeypatch):
    crypto.put(SLOT, NAME, VALUE)
    _set_key(monkeypatch, Fernet.generate_key().decode())
    with pytest.raises(crypto.SecretsUnreadable) as e:
        crypto.get(SLOT, NAME)
    assert VALUE not in str(e.value)
    assert crypto.state(SLOT, NAME) == "unreadable"
    crypto.put(SLOT, NAME, "re-entered")                     # re-entering fixes it
    assert crypto.get(SLOT, NAME) == "re-entered"


def test_a_row_moved_to_another_name_does_not_decrypt_as_that_secret(secrets_key):
    crypto.put(SLOT, NAME, VALUE)
    with db.connect() as conn:
        conn.execute("UPDATE plugin_secrets SET name = 'other'")
    with pytest.raises(crypto.SecretsUnreadable):
        crypto.get(SLOT, "other")


def test_rows_without_a_key_read_as_unavailable(secrets_key, monkeypatch):
    crypto.put(SLOT, NAME, VALUE)
    _set_key(monkeypatch, None)
    with pytest.raises(crypto.SecretsUnavailable):
        crypto.get(SLOT, NAME)
    assert crypto.state(SLOT, NAME) == "unreadable"


def test_delete_needs_no_key(secrets_key, monkeypatch):
    crypto.put(SLOT, NAME, VALUE)
    _set_key(monkeypatch, None)
    assert crypto.delete(SLOT, NAME) is True
    assert crypto.delete(SLOT, NAME) is False
    assert crypto.state(SLOT, NAME) == "unset"


def test_malformed_key_is_unavailable_and_never_echoed(env, monkeypatch):
    _set_key(monkeypatch, "not-a-fernet-key-but-secret")
    assert not crypto.key_configured()
    with pytest.raises(crypto.SecretsUnavailable) as e:
        crypto.put(SLOT, NAME, VALUE)
    assert "not-a-fernet-key-but-secret" not in str(e.value)


# ---- boot ---------------------------------------------------------------------------

def test_boot_without_rows_or_key_is_fine(env):
    crypto.check_boot()


def test_boot_fails_closed_when_rows_exist_and_the_key_is_missing(secrets_key, monkeypatch):
    crypto.put(SLOT, NAME, VALUE)
    _set_key(monkeypatch, None)
    with pytest.raises(RuntimeError) as e:
        crypto.check_boot()
    assert "BROKER_SECRETS_KEY" in str(e.value)


def test_boot_fails_closed_on_a_malformed_key(env, monkeypatch):
    _set_key(monkeypatch, "garbage-key-value")
    with pytest.raises(RuntimeError) as e:
        crypto.check_boot()
    assert "garbage-key-value" not in str(e.value)


def test_boot_with_an_unreadable_row_warns_by_name_only(secrets_key, monkeypatch, caplog):
    crypto.put(SLOT, NAME, VALUE)
    new_key = Fernet.generate_key().decode()
    _set_key(monkeypatch, new_key)
    with caplog.at_level(logging.WARNING, logger="broker.crypto"):
        crypto.check_boot()                                  # boots: re-enter from the console
    assert f"{SLOT}/{NAME}" in caplog.text
    assert VALUE not in caplog.text and new_key not in caplog.text


def test_app_refuses_to_start_with_orphaned_secrets(secrets_key, monkeypatch):
    crypto.put(SLOT, NAME, VALUE)
    _set_key(monkeypatch, None)
    from broker.main import app
    with pytest.raises(RuntimeError):
        with TestClient(app):
            pass
