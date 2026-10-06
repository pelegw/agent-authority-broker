"""broker.db schema: every planned table exists, init is idempotent, and the
additive migration mechanism works, and WAL is switched on once, by init(),
so concurrent first connections never race each other into a lock error."""

import contextlib
import sqlite3
import threading

import pytest

from broker import db
from broker.config import get_settings

EXPECTED_TABLES = {
    "principals", "sessions", "admin_tokens", "api_keys", "grants", "plugins",
    "plugin_secrets", "hidden_resources", "actions", "decisions",
    "capacity_ledger", "ledger_grants", "audit_log", "app_config", "plugin_pins",
}


def _tables() -> set[str]:
    with db.connect() as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r["name"] for r in rows}


def _columns(table: str) -> set[str]:
    with db.connect() as conn:
        return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_init_creates_every_table_and_is_idempotent(env):
    db.init()   # the env fixture already ran it once; a second run must not fail
    db.init()
    assert EXPECTED_TABLES <= _tables()
    assert set(db.TABLES) == EXPECTED_TABLES


def test_chain_and_actor_columns_exist(env):
    assert {"actor_principal", "actor_via"} <= _columns("audit_log")
    assert {"parent_key_id", "created_by", "denies", "principal_id"} <= _columns("api_keys")
    assert {"parent_grant_id", "kind", "capabilities", "decided_via"} <= _columns("grants")
    assert {"grant_chain", "prev_hash", "hash", "signed"} <= _columns("decisions")


def test_no_principal_is_seeded(env):
    # Owner creation is phase 1's setup flow, never an implicit default.
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM principals").fetchone()[0] == 0


def test_app_config_roundtrip(env):
    assert db.get_config("missing") is None
    assert db.get_config("missing", "dflt") == "dflt"
    db.set_config("setup_completed", "1")
    db.set_config("setup_completed", "2")   # upsert, not a duplicate
    assert db.get_config("setup_completed") == "2"


def test_migrations_add_missing_columns_once(env, monkeypatch):
    monkeypatch.setattr(db, "_MIGRATIONS", {"plugins": {"extra_note": "TEXT"}})
    db.init()
    db.init()   # column already there: must not raise "duplicate column"
    assert "extra_note" in _columns("plugins")


def test_wal_mode(env):
    with db.connect() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


# ---- concurrency: WAL is switched on once, by init() ----------------------------------

def _fresh_db(monkeypatch, path) -> None:
    monkeypatch.setenv("BROKER_DB", str(path))
    get_settings.cache_clear()


def test_concurrent_first_connections_do_not_lock(env, tmp_path, monkeypatch):
    # Regression: connect() used to run `PRAGMA journal_mode=WAL` on every
    # connection. On a new file that switch needs exclusive access, and when
    # two connections attempt it at once SQLite fails one of them at once
    # ("database is locked", no busy wait). The suite hit exactly this: a
    # stray connection racing the next test's db.init(). Here threads read
    # and write a fresh file while one of them runs init().
    errors: list[str] = []

    def reader_writer(barrier, i):
        barrier.wait()
        try:
            with contextlib.closing(db.connect()) as conn:
                conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
                with conn:
                    conn.execute("CREATE TABLE IF NOT EXISTS probe (i INTEGER)")
                    conn.execute("INSERT INTO probe VALUES (?)", (i,))
                conn.execute("SELECT count(*) FROM probe").fetchone()
        except Exception as exc:          # collected: a thread must not fail silently
            errors.append(f"{type(exc).__name__}: {exc}")

    def initializer(barrier):
        barrier.wait()
        try:
            db.init()
        except Exception as exc:
            errors.append(f"init {type(exc).__name__}: {exc}")

    for round_no in range(15):
        _fresh_db(monkeypatch, tmp_path / f"fresh{round_no}.db")
        barrier = threading.Barrier(6)
        threads = [threading.Thread(target=initializer, args=(barrier,))]
        threads += [threading.Thread(target=reader_writer, args=(barrier, i)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        assert errors == [], errors
        with contextlib.closing(db.connect()) as conn:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
            assert conn.execute("SELECT count(*) FROM probe").fetchone()[0] == 5
            assert set(db.TABLES) <= {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}


def test_connect_does_not_switch_the_journal_mode(env, tmp_path, monkeypatch):
    # Only init() switches: a connection to a file init() never saw keeps
    # SQLite's default, which is what keeps first connections from racing.
    _fresh_db(monkeypatch, tmp_path / "untouched.db")
    with contextlib.closing(db.connect()) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal"
    db.init()
    with contextlib.closing(db.connect()) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


class _BusyThenOk:
    """A connection stand-in whose WAL switch reports SQLITE_BUSY `busy` times."""

    def __init__(self, busy: int, code: int | None = None):
        self.calls, self._busy = 0, busy
        self._code = sqlite3.SQLITE_BUSY if code is None else code

    def execute(self, sql):
        self.calls += 1
        if self.calls <= self._busy:
            exc = sqlite3.OperationalError("database is locked")
            exc.sqlite_errorcode = self._code
            raise exc
        return self

    def fetchone(self):
        return ("wal",)


def test_the_wal_switch_retries_while_busy():
    # SQLite does not wait for a contended mode switch itself (it fails one
    # side at once to avoid a deadlock), so init() does the waiting.
    conn = _BusyThenOk(busy=3)
    assert db._enable_wal(conn) == "wal"
    assert conn.calls == 4


def test_the_wal_switch_gives_up_after_the_busy_timeout(monkeypatch):
    monkeypatch.setattr(db, "BUSY_TIMEOUT_SECONDS", 0.05)
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        db._enable_wal(_BusyThenOk(busy=10**9))


def test_the_wal_switch_does_not_retry_other_errors():
    conn = _BusyThenOk(busy=1, code=sqlite3.SQLITE_IOERR)
    with pytest.raises(sqlite3.OperationalError):
        db._enable_wal(conn)
    assert conn.calls == 1
