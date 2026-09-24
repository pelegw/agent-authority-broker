"""Broker-side secret store: Fernet under BROKER_SECRETS_KEY, rows in the
`plugin_secrets` table.

What lives here is only what the owner enters in the console and the broker
itself must use, e.g. the Telegram bot token (slot `broker`, name
`telegram_bot_token`). Target credentials never do: each plugin service keeps
its own under its own key. This module is the only reader and writer of
`plugin_secrets`, so "secrets only via crypto.py" is checkable by grep.

Failure modes, all closed:
  * rows exist but BROKER_SECRETS_KEY is missing, or the key is malformed:
    `check_boot()` raises and the broker refuses to start, instead of
    quietly behaving as if no secret had ever been entered;
  * the key was rotated or replaced: a row no longer decrypts. `get` raises
    `SecretsUnreadable`; callers treat the secret as absent (Telegram stays
    off) and the console shows "re-enter required". Nothing crashes.

Each plaintext is bound to its (slot, name) before encryption, so a row
copied onto another slot or name does not decrypt as that secret. Values
are never logged, printed, or returned by any admin route.
"""

from __future__ import annotations

import logging
import time

from cryptography.fernet import Fernet, InvalidToken

from . import db
from .config import get_settings

log = logging.getLogger(__name__)

# The slot for secrets the broker itself uses (as opposed to a plugin id).
BROKER_SLOT = "broker"
_SEP = "\x00"


class SecretsUnavailable(Exception):
    """BROKER_SECRETS_KEY is not configured (or malformed): nothing can be
    stored, and nothing stored can be read."""


class SecretsUnreadable(Exception):
    """A stored secret does not decrypt under the current key (the key was
    rotated or replaced). The owner must re-enter it."""


def _fernet() -> Fernet:
    key = get_settings().broker_secrets_key
    if not key:
        raise SecretsUnavailable("BROKER_SECRETS_KEY is not set")
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        # Never echo the key itself, not even a malformed one.
        raise SecretsUnavailable("BROKER_SECRETS_KEY is not a valid Fernet key") from exc


def key_configured() -> bool:
    """True when a usable BROKER_SECRETS_KEY is present."""
    try:
        _fernet()
    except SecretsUnavailable:
        return False
    return True


def _bind(slot: str, name: str, value: str) -> bytes:
    return f"{slot}{_SEP}{name}{_SEP}{value}".encode("utf-8")


def put(slot: str, name: str, value: str) -> None:
    """Encrypt and store (insert or replace) one secret."""
    if not isinstance(value, str) or not value:
        raise ValueError("a secret must be a non-empty string")
    token = _fernet().encrypt(_bind(slot, name, value))
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO plugin_secrets (slot, name, ciphertext, updated_at)"
            " VALUES (?, ?, ?, ?) ON CONFLICT(slot, name) DO UPDATE SET"
            " ciphertext = excluded.ciphertext, updated_at = excluded.updated_at",
            (slot, name, token, int(time.time())))


def _row(slot: str, name: str):
    with db.connect() as conn:
        return conn.execute("SELECT ciphertext, updated_at FROM plugin_secrets"
                            " WHERE slot = ? AND name = ?", (slot, name)).fetchone()


def get(slot: str, name: str) -> str | None:
    """The plaintext, or None when nothing is stored.

    Raises SecretsUnavailable when a row exists but no key is configured, and
    SecretsUnreadable when the row does not decrypt (or is bound to another
    slot/name). Never returns a partial or unverified value.
    """
    row = _row(slot, name)
    if row is None:
        return None
    f = _fernet()
    try:
        plain = f.decrypt(bytes(row["ciphertext"])).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError, TypeError) as exc:
        raise SecretsUnreadable(f"{slot}/{name} does not decrypt under the current key") from exc
    prefix = f"{slot}{_SEP}{name}{_SEP}"
    if not plain.startswith(prefix):
        raise SecretsUnreadable(f"{slot}/{name} is bound to another slot or name")
    return plain[len(prefix):]


def delete(slot: str, name: str) -> bool:
    """Remove one secret. Works without the key (clearing needs no decrypt)."""
    with db.connect() as conn:
        return conn.execute("DELETE FROM plugin_secrets WHERE slot = ? AND name = ?",
                            (slot, name)).rowcount > 0


def state(slot: str, name: str) -> str:
    """"unset" | "set" | "unreadable" (stored but not decryptable now).

    For status views: tells the owner whether to re-enter a secret without
    ever touching its value outside this module.
    """
    if _row(slot, name) is None:
        return "unset"
    try:
        get(slot, name)
    except (SecretsUnavailable, SecretsUnreadable):
        return "unreadable"
    return "set"


def check_boot() -> None:
    """Fail closed at startup when stored secrets could never be read.

    Rows present + key missing or malformed = refuse to boot: silently
    running as if the owner had never configured anything would switch
    channels off without anyone noticing. A malformed key alone (no rows)
    also refuses, since every later write would fail. Rows that merely do
    not decrypt (key replaced) are logged by name and left for the console's
    "re-enter required" state.
    """
    with db.connect() as conn:
        rows = conn.execute("SELECT slot, name FROM plugin_secrets ORDER BY slot, name").fetchall()
    key = get_settings().broker_secrets_key
    if not key:
        if rows:
            raise RuntimeError(
                f"plugin_secrets holds {len(rows)} encrypted value(s) but BROKER_SECRETS_KEY "
                "is not set. Restore the key in .env (python scripts/init_secrets.py writes "
                "one), or clear the values from the console before removing the key.")
        return
    try:
        _fernet()
    except SecretsUnavailable as exc:
        raise RuntimeError(f"{exc}; regenerate it with "
                           "python scripts/init_secrets.py --rotate BROKER_SECRETS_KEY") from exc
    unreadable = [f"{r['slot']}/{r['name']}" for r in rows
                  if state(r["slot"], r["name"]) == "unreadable"]
    if unreadable:
        # Names only; the console shows these as "re-enter required".
        log.warning("secrets that no longer decrypt under BROKER_SECRETS_KEY "
                    "(re-enter them in the console): %s", ", ".join(unreadable))
