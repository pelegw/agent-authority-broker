"""broker.db schema: every planned table exists, init is idempotent, and the
additive migration mechanism works."""

from broker import db

EXPECTED_TABLES = {
    "principals", "sessions", "admin_tokens", "api_keys", "grants", "plugins",
    "plugin_secrets", "hidden_resources", "actions", "decisions",
    "capacity_ledger", "ledger_grants", "audit_log", "app_config",
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
