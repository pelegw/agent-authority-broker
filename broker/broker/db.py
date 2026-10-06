"""broker.db is the broker's own state: owner, keys, grants, plugins, queued
actions, the decision record, the capacity ledger, and the ops audit log.

Target data is NOT here (the WhatsApp archive belongs to the sidecar; GitHub
and Google data stay at the target). Connections are cheap per-operation
sqlite3 handles; a single uvicorn worker keeps write concurrency trivial.

Schema changes are additive only: new tables go in SCHEMA (CREATE ... IF NOT
EXISTS), new columns on existing tables go in _MIGRATIONS so an in-place
upgrade never requires recreating the database.

Timestamps are unix seconds (INTEGER) throughout. JSON columns hold compact
JSON text; the owning module is the only writer of each JSON shape.

The file runs in WAL mode, switched on ONCE by init(). journal_mode=WAL is
persistent (it is recorded in the file header), so connect() never repeats
it: on a new file the switch needs exclusive access, and when two
connections attempt it at once SQLite fails one of them immediately
("database is locked", skipping the busy wait to avoid a deadlock). A
per-connect pragma turned any two first connections into that race.
"""

import contextlib
import sqlite3
import time

from .config import get_settings

SCHEMA = """
-- The humans the broker acts for. v0.2 has exactly one row (the owner), but
-- every other table carries principal_id so multi-principal is additive.
CREATE TABLE IF NOT EXISTS principals (
    id            TEXT PRIMARY KEY,           -- uuid4
    username      TEXT NOT NULL UNIQUE,       -- what decisions/audit record as the actor
    display_name  TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL,              -- hashlib.scrypt hex
    password_salt TEXT NOT NULL,              -- per-user random salt, hex
    created_at    INTEGER NOT NULL,
    disabled      INTEGER NOT NULL DEFAULT 0
);

-- Console login sessions (cookie auth). Expiry is the earlier of the idle and
-- absolute limits; last_seen_at drives the idle check.
CREATE TABLE IF NOT EXISTS sessions (
    id           TEXT PRIMARY KEY,            -- sha256 of the cookie value, never the cookie itself
    principal_id TEXT NOT NULL,
    created_at   INTEGER NOT NULL,
    expires_at   INTEGER NOT NULL,            -- absolute cap (session_absolute_seconds)
    last_seen_at INTEGER NOT NULL,            -- idle cap is measured from here
    ip           TEXT NOT NULL DEFAULT '',
    user_agent   TEXT NOT NULL DEFAULT ''
);

-- Owner-minted bearer tokens (aab_admin_...) for the CLI, scripts, and deploys.
CREATE TABLE IF NOT EXISTS admin_tokens (
    id           TEXT PRIMARY KEY,            -- uuid4
    principal_id TEXT NOT NULL,
    name         TEXT NOT NULL,
    token_hash   TEXT NOT NULL UNIQUE,        -- sha256 hex; the plaintext is shown once
    created_at   INTEGER NOT NULL,
    expires_at   INTEGER,                     -- NULL = never
    last_used_at INTEGER,
    revoked      INTEGER NOT NULL DEFAULT 0
    -- scope ('admin' | 'monitor') arrived after the first release: _MIGRATIONS.
);

-- Agent keys (aab_...). A key holds no authority by itself: authority comes
-- from its grants, capped by its role and the owner's ceiling.
CREATE TABLE IF NOT EXISTS api_keys (
    id              INTEGER PRIMARY KEY,
    principal_id    TEXT NOT NULL,            -- whose authority this key spends
    name            TEXT NOT NULL UNIQUE,
    key_hash        TEXT NOT NULL UNIQUE,     -- sha256 hex of the current key
    prev_key_hash   TEXT,                     -- previous secret during rotation grace
    prev_expires_at INTEGER,                  -- when the previous secret stops working
    role            TEXT NOT NULL DEFAULT 'read-only',  -- read-only|read-draft|read-act|full
    rate_per_min    INTEGER NOT NULL DEFAULT 6,
    disabled        INTEGER NOT NULL DEFAULT 0,
    expires_at      INTEGER,                  -- NULL = never expires
    created_at      INTEGER NOT NULL,
    last_used_at    INTEGER,                  -- throttled: updated at most ~1/min
    last_used_ip    TEXT,
    parent_key_id   INTEGER,                  -- NULL = root key; else the delegating key
    created_by      TEXT NOT NULL DEFAULT 'owner',       -- owner | delegation
    -- Per-key deny sets {target: {kind: [ids]}}. Lives outside the grant
    -- lattice; a delegated child's effective denies = parent's UNION its own.
    denies          TEXT NOT NULL DEFAULT '{}'
);

-- The only authority object. capabilities is a JSON list of allow-statements;
-- a child grant (parent_grant_id set) is only ever produced by narrow().
CREATE TABLE IF NOT EXISTS grants (
    id                   TEXT PRIMARY KEY,    -- uuid4
    principal_id         TEXT NOT NULL,
    key_id               INTEGER NOT NULL,
    parent_grant_id      TEXT,                -- NULL for root grants
    kind                 TEXT NOT NULL,       -- root | expansion | delegation
    capabilities         TEXT NOT NULL DEFAULT '[]',
    status               TEXT NOT NULL DEFAULT 'pending',  -- pending|active|rejected|expired|revoked
    reason               TEXT NOT NULL DEFAULT '',  -- agent's stated reason (expansion/delegation)
    created_at           INTEGER NOT NULL,
    decided_at           INTEGER,
    decided_by_principal TEXT,                -- username of the human who decided
    decided_via          TEXT,                -- session | token | telegram
    expires_at           INTEGER,             -- NULL = never
    requested_by_key_id  INTEGER              -- agent that asked (expansion) or delegated
);

-- One row per discovered plugin manifest; disabled on first boot.
CREATE TABLE IF NOT EXISTS plugins (
    id          TEXT PRIMARY KEY,             -- manifest id: ^[a-z][a-z0-9]*$
    enabled     INTEGER NOT NULL DEFAULT 0,
    config      TEXT NOT NULL DEFAULT '{}',   -- non-secret only; secrets go to the plugin's /configure
    connected   INTEGER NOT NULL DEFAULT 0,   -- also 0 when secrets fail to decrypt (reconnect required)
    last_health TEXT NOT NULL DEFAULT '{}',   -- JSON from adapter.status()
    updated_at  INTEGER NOT NULL
);

-- Broker-side secrets the owner enters in the console and the broker itself
-- uses, e.g. the Telegram bot token (slot 'broker', name 'telegram_bot_token').
-- Read and written only through crypto.py, Fernet under BROKER_SECRETS_KEY.
-- Target credentials never live here: the Google refresh token and GitHub App
-- key stay in each plugin's own secret volume under its own
-- PLUGIN_SECRETS_KEY_<SERVICE>, the WhatsApp session in wa_session (sidecar-only).
CREATE TABLE IF NOT EXISTS plugin_secrets (
    slot       TEXT NOT NULL,                 -- 'broker' for the broker's own secrets
    name       TEXT NOT NULL,
    ciphertext BLOB NOT NULL,                 -- Fernet token under BROKER_SECRETS_KEY
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (slot, name)
);

-- Owner-level denies: resources no key may see. Hidden == 404 to agents.
-- label is DISPLAY-ONLY (captured at add time): enforcement is always by
-- resource_id, so renaming a resource can never unhide it.
CREATE TABLE IF NOT EXISTS hidden_resources (
    target      TEXT NOT NULL,                -- plugin id
    kind        TEXT NOT NULL,                -- manifest resource kind (chat, repo, label, ...)
    resource_id TEXT NOT NULL,                -- normalized id
    label       TEXT NOT NULL DEFAULT '',
    reason      TEXT NOT NULL DEFAULT '',
    created_at  INTEGER NOT NULL,
    PRIMARY KEY (target, kind, resource_id)
);

-- Queued actions: drafts awaiting a human, and approved/automatic actions
-- scheduled for later. Status moves only via atomic UPDATE ... WHERE status=?.
CREATE TABLE IF NOT EXISTS actions (
    id                   TEXT PRIMARY KEY,    -- uuid4
    principal_id         TEXT NOT NULL,
    key_id               INTEGER NOT NULL,
    target               TEXT NOT NULL,
    action               TEXT NOT NULL,       -- manifest action name (without the target prefix)
    params               TEXT NOT NULL DEFAULT '{}',
    resource_label       TEXT NOT NULL DEFAULT '',  -- human-readable resource for cards/console
    note                 TEXT NOT NULL DEFAULT '',  -- agent rationale, shown to the human
    -- pending|scheduled|sending|done|rejected|expired|canceled|failed
    status               TEXT NOT NULL DEFAULT 'pending',
    approval_source      TEXT,                -- human | automatic (automatic re-evaluates at delivery)
    created_at           INTEGER NOT NULL,
    expires_at           INTEGER,             -- pending drafts expire
    decided_at           INTEGER,
    decided_by_principal TEXT,
    decided_via          TEXT,                -- session | token | telegram
    run_at               INTEGER,             -- NULL = deliver on approval
    decision_id          INTEGER,             -- decisions.id of the originating decision
    result               TEXT                 -- JSON adapter result or error
);

-- The accountability record. Append-only and hash-chained: each row's hash is
-- an HMAC over prev_hash plus the canonical row (see decisions.py). A decision
-- row is written BEFORE any side effect; an outcome row follows it.
CREATE TABLE IF NOT EXISTS decisions (
    id              INTEGER PRIMARY KEY,
    request_id      TEXT NOT NULL,            -- links a decision to its outcome
    kind            TEXT NOT NULL,            -- decision | outcome
    ts              INTEGER NOT NULL,
    principal_id    TEXT,
    key_id          INTEGER,
    key_name        TEXT,                     -- denormalized: survives key deletion
    grant_chain     TEXT NOT NULL DEFAULT '[]',  -- JSON grant ids, root -> leaf
    target          TEXT NOT NULL,
    action          TEXT NOT NULL,
    resource        TEXT NOT NULL DEFAULT '',
    params_hash     TEXT NOT NULL DEFAULT '', -- sha256 of canonical params; params themselves not stored
    decision        TEXT,                     -- allow | draft | deny (NULL on outcome rows)
    reason          TEXT NOT NULL DEFAULT '',
    enforced_where  TEXT NOT NULL DEFAULT '{}',  -- JSON {dimension: target|proxy}
    outcome         TEXT,                     -- NULL on decision rows
    actor_principal TEXT,                     -- human who approved, when one did
    actor_via       TEXT,                     -- session | token | telegram
    prev_hash       TEXT NOT NULL,
    hash            TEXT NOT NULL,
    signed          INTEGER NOT NULL DEFAULT 0  -- 1 = HMAC with DECISION_SIGNING_KEY
);

-- Budget accounting. A reservation is taken before a write and committed or
-- released after the adapter answers (503 releases; 502 keeps it).
CREATE TABLE IF NOT EXISTS capacity_ledger (
    id     INTEGER PRIMARY KEY,
    ts     INTEGER NOT NULL,
    key_id INTEGER NOT NULL,
    target TEXT NOT NULL,
    action TEXT NOT NULL,
    state  TEXT NOT NULL DEFAULT 'reserved'   -- reserved | committed | released
);

-- One charge per grant in the chain, so a parent's per_day budget bounds its
-- whole delegated subtree.
CREATE TABLE IF NOT EXISTS ledger_grants (
    ledger_id INTEGER NOT NULL,
    grant_id  TEXT NOT NULL,
    PRIMARY KEY (ledger_id, grant_id)
);

-- Admin/ops events only (logins, key changes, plugin toggles). Never
-- load-bearing: nothing reads it to make a policy decision.
CREATE TABLE IF NOT EXISTS audit_log (
    id              INTEGER PRIMARY KEY,
    ts              INTEGER NOT NULL,
    actor           TEXT NOT NULL,            -- API key name, owner username, or 'system'
    action          TEXT NOT NULL,            -- e.g. auth.login, key.create, plugin.enable
    resource        TEXT NOT NULL DEFAULT '',
    detail          TEXT NOT NULL DEFAULT '', -- JSON blob
    result          TEXT NOT NULL DEFAULT 'ok',
    actor_principal TEXT,                     -- principal id when a human acted
    actor_via       TEXT                      -- session | token | telegram
);

-- Runtime, admin-managed key/value config (setup_completed, telegram_*,
-- operator settings edited from the console). OAuth state nonces live in
-- the plugin services, never here.
CREATE TABLE IF NOT EXISTS app_config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_principal ON sessions(principal_id);
CREATE INDEX IF NOT EXISTS idx_admin_tokens_principal ON admin_tokens(principal_id);
CREATE INDEX IF NOT EXISTS idx_api_keys_principal ON api_keys(principal_id);
CREATE INDEX IF NOT EXISTS idx_api_keys_parent ON api_keys(parent_key_id);
CREATE INDEX IF NOT EXISTS idx_grants_key_status ON grants(key_id, status);
CREATE INDEX IF NOT EXISTS idx_grants_parent ON grants(parent_grant_id);
CREATE INDEX IF NOT EXISTS idx_grants_status ON grants(status);
CREATE INDEX IF NOT EXISTS idx_actions_status_run ON actions(status, run_at);
CREATE INDEX IF NOT EXISTS idx_actions_key ON actions(key_id);
CREATE INDEX IF NOT EXISTS idx_decisions_request ON decisions(request_id);
CREATE INDEX IF NOT EXISTS idx_decisions_key_ts ON decisions(key_id, ts);
CREATE INDEX IF NOT EXISTS idx_ledger_key_ts ON capacity_ledger(key_id, ts);
CREATE INDEX IF NOT EXISTS idx_ledger_grants_grant ON ledger_grants(grant_id);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
"""

# Every table SCHEMA creates. Tests assert these exist after init().
TABLES = (
    "principals", "sessions", "admin_tokens", "api_keys", "grants", "plugins",
    "plugin_secrets", "hidden_resources", "actions", "decisions",
    "capacity_ledger", "ledger_grants", "audit_log", "app_config",
)


# How long a statement waits for another connection's lock before failing.
BUSY_TIMEOUT_SECONDS = 10.0


def connect() -> sqlite3.Connection:
    # timeout= installs SQLite's busy handler before the first statement runs.
    # No journal_mode pragma here: init() sets WAL once (see the docstring).
    conn = sqlite3.connect(get_settings().broker_db, timeout=BUSY_TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row
    return conn


def _enable_wal(conn: sqlite3.Connection) -> str:
    """Put the file in WAL mode (a no-op once it is). Returns the mode.

    Switching a new file to WAL upgrades a read lock to an exclusive one.
    When another connection is mid-switch or mid-write, SQLite fails that
    upgrade at once with SQLITE_BUSY instead of waiting (the busy handler is
    skipped to avoid a deadlock), so the waiting is done here, bounded by the
    same timeout every other statement gets.
    """
    deadline = time.monotonic() + BUSY_TIMEOUT_SECONDS
    while True:
        try:
            return conn.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower()
        except sqlite3.OperationalError as exc:
            busy = (getattr(exc, "sqlite_errorcode", 0) & 0xFF) == sqlite3.SQLITE_BUSY
            if not busy or time.monotonic() >= deadline:
                raise
            time.sleep(0.02)


# Columns added after the first release, as {table: {column: declaration}};
# applied to pre-existing databases so an in-place upgrade doesn't require
# recreating broker.db. Additive only: never rename or drop here.
_MIGRATIONS: dict[str, dict[str, str]] = {
    # admin: the CLI, scripts and deploys (every token before this column).
    # monitor: /health and /v1/health only (identity/admin_tokens.py).
    "admin_tokens": {"scope": "TEXT NOT NULL DEFAULT 'admin'"},
}


def _migrate(conn) -> None:
    for table, columns in _MIGRATIONS.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, decl in columns.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def init() -> str:
    """Switch the file to WAL, create tables if missing and add any new
    columns. Called at startup, before anything else opens the database.
    Returns the journal mode (`wal` unless the filesystem refused it), which
    the boot log reports."""
    with contextlib.closing(connect()) as conn, conn:
        mode = _enable_wal(conn)
        conn.executescript(SCHEMA)
        _migrate(conn)
    return mode


# ---- runtime key/value config (app_config) --------------------------------

def get_config(key: str, default: str | None = None) -> str | None:
    with connect() as conn:
        row = conn.execute("SELECT value FROM app_config WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_config(key: str, value: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO app_config (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
