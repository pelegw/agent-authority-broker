"""Principals: the humans the broker acts for, and their password hashing.

v0.2 has exactly one principal (the owner). The table is multi-row-ready, but
`create_owner` refuses a second row so a single-owner deployment can never
grow a second admin by accident.

Passwords are hashed with scrypt (memory-hard, stdlib) under a per-user random
salt. Verification always runs scrypt, even for an unknown username, so the
response time does not reveal whether a username exists.
"""

import hashlib
import hmac
import re
import secrets
import sqlite3
import time
import uuid
from dataclasses import dataclass

from .. import db
from ..errors import PolicyError

# scrypt cost: n=2^15, r=8 needs 128*r*n = 32 MiB of memory per hash, which is
# exactly OpenSSL's default maxmem ceiling, so we raise the ceiling explicitly.
_SCRYPT = dict(n=2 ** 15, r=8, p=1, dklen=64, maxmem=64 * 1024 * 1024)
_SALT_BYTES = 16

USERNAME_RE = re.compile(r"^[a-z0-9_.-]{3,32}$")
MIN_PASSWORD = 12
# An upper bound keeps a multi-megabyte "password" from being a cheap DoS on
# the hashing path; nobody types more than this.
MAX_PASSWORD = 1024

# Dummy salt/hash compared against when the username is unknown, so that path
# does the same scrypt work as a real verification.
_DUMMY_SALT = secrets.token_hex(_SALT_BYTES)
_DUMMY_HASH = "0" * 128


@dataclass(frozen=True)
class Principal:
    id: str
    username: str
    disabled: bool


class OwnerExists(Exception):
    """A principal already exists; v0.2 is single-owner."""


def hash_password(password: str, salt_hex: str) -> str:
    """scrypt(password, salt) as hex."""
    return hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex),
                          **_SCRYPT).hex()


def validate_username(username: str) -> None:
    if not isinstance(username, str) or not USERNAME_RE.match(username):
        raise PolicyError(400, "username must match ^[a-z0-9_.-]{3,32}$", "invalid_username")


def validate_password(password: str) -> None:
    if not isinstance(password, str) or len(password) < MIN_PASSWORD:
        raise PolicyError(400, f"password must be at least {MIN_PASSWORD} characters",
                          "weak_password")
    if len(password) > MAX_PASSWORD:
        raise PolicyError(400, f"password must be at most {MAX_PASSWORD} characters",
                          "invalid_password")


def any_exists() -> bool:
    with db.connect() as conn:
        return conn.execute("SELECT 1 FROM principals LIMIT 1").fetchone() is not None


def create_owner(username: str, password: str) -> Principal:
    """Create the single owner. Raises OwnerExists if any principal exists.

    The existence check and the insert share one IMMEDIATE transaction, so two
    concurrent setup requests cannot both create an owner.
    """
    validate_username(username)
    validate_password(password)
    salt = secrets.token_hex(_SALT_BYTES)
    pw_hash = hash_password(password, salt)   # outside the write lock: it is slow
    pid = str(uuid.uuid4())
    conn = db.connect()
    try:
        conn.isolation_level = None   # manage the transaction by hand
        conn.execute("BEGIN IMMEDIATE")
        try:
            if conn.execute("SELECT 1 FROM principals LIMIT 1").fetchone():
                raise OwnerExists()
            conn.execute(
                "INSERT INTO principals (id, username, password_hash, password_salt,"
                " created_at) VALUES (?, ?, ?, ?, ?)",
                (pid, username, pw_hash, salt, int(time.time())))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    except sqlite3.IntegrityError as e:   # UNIQUE(username); unreachable with one row
        raise OwnerExists() from e
    finally:
        conn.close()
    return Principal(pid, username, False)


def _check(row, password: str) -> bool:
    """Constant-work password check against a row (or the dummy when row is None)."""
    salt = row["password_salt"] if row else _DUMMY_SALT
    expected = row["password_hash"] if row else _DUMMY_HASH
    supplied = hash_password(password, salt)
    ok = hmac.compare_digest(supplied, expected)
    return ok and row is not None


def verify_password(username: str, password: str) -> Principal | None:
    """The enabled principal whose credentials these are, else None.

    Always performs exactly one scrypt, whether or not the username exists or
    the principal is disabled, so timing reveals nothing about either.
    """
    if not isinstance(password, str) or len(password) > MAX_PASSWORD:
        password = ""   # still hash something: an over-long input is not a shortcut
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM principals WHERE username = ?",
                           (username,)).fetchone()
    if not _check(row, password) or row["disabled"]:
        return None
    return Principal(row["id"], row["username"], bool(row["disabled"]))


def check_password(principal_id: str, password: str) -> bool:
    """True when `password` is this principal's current password."""
    if not isinstance(password, str) or len(password) > MAX_PASSWORD:
        password = ""
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM principals WHERE id = ?",
                           (principal_id,)).fetchone()
    return _check(row, password)


def set_password(principal_id: str, new_password: str) -> None:
    """Replace the password (fresh salt). Callers handle session invalidation."""
    validate_password(new_password)
    salt = secrets.token_hex(_SALT_BYTES)
    pw_hash = hash_password(new_password, salt)
    with db.connect() as conn:
        conn.execute("UPDATE principals SET password_hash = ?, password_salt = ? WHERE id = ?",
                     (pw_hash, salt, principal_id))


def get(principal_id: str) -> Principal | None:
    with db.connect() as conn:
        row = conn.execute("SELECT id, username, disabled FROM principals WHERE id = ?",
                           (principal_id,)).fetchone()
    return Principal(row["id"], row["username"], bool(row["disabled"])) if row else None
