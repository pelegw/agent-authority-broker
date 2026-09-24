"""A throwaway broker for the simulation: temp DB, echo plugin, owner, key.

It reuses the test fixtures' machinery (conftest's `register_inprocess` and
`enable_plugin`, the vendored echo manifest) rather than a parallel setup
path, so the simulation exercises exactly what the suite exercises. It runs
outside pytest too (`python -m tests.simulation.simulate`), so it sets and
restores the process globals itself instead of relying on monkeypatch.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path

from broker import auth, db, ledger, notify
from broker.authority import store
from broker.authority.capability import from_json, normalize_all
from broker.config import get_settings
from broker.deps import AdminContext
from broker.identity import principals
from broker.plugins import registry

from ..conftest import ECHO_DIR, enable_plugin, register_inprocess
from ..fixtures.echo.adapter import EchoAdapter

OWNER = "owner"
OWNER_PASSWORD = "simulation owner password"
# Simulated hours run in real seconds, so the key's real-time per-minute
# limiter would trip on compressed time that no real agent produces. It is
# set out of reach; the grant's per-day budget (a 24 h window that an 8 h
# day fits inside either way) is the limit the simulation measures.
UNTHROTTLED = 1_000_000


@contextmanager
def sandbox(on_disk: bool = False):
    """Fresh DB + registry with echo enabled and connected; restores the
    environment, settings cache, registry, notifiers and `db.connect` on exit.

    By default the database is a private shared-cache in-memory SQLite DB:
    the same schema, statements and transactions, minus the file I/O, which
    on Windows made a one-hour workload take a minute (every broker call
    opens several connections). The simulation measures authority
    decisions, not disk. `on_disk=True` uses a real temp file instead, to
    check that the counts do not depend on the storage medium."""
    saved_db = os.environ.get("BROKER_DB")
    saved_providers, saved_connect = notify._PROVIDERS, db.connect
    anchor = None
    with tempfile.TemporaryDirectory(prefix="aab-sim-", ignore_cleanup_errors=True) as tmp:
        os.environ["BROKER_DB"] = str(Path(tmp) / "broker.db")
        get_settings.cache_clear()
        if not on_disk:
            uri = f"file:aab-sim-{uuid.uuid4().hex}?mode=memory&cache=shared"
            # A shared in-memory DB lives only while a connection holds it.
            anchor = sqlite3.connect(uri, uri=True)
            db.connect = _memory_connect(uri)
        notify._PROVIDERS = []            # no Telegram cards from a simulation
        try:
            db.init()
            registry.reset_registry(registry.Registry(vendored_dirs=(ECHO_DIR.parent,)))
            ledger.rate_limiter.reset()
            register_inprocess(EchoAdapter())
            enable_plugin()
            yield
        finally:
            registry.reset_registry()
            ledger.rate_limiter.reset()
            notify._PROVIDERS = saved_providers
            db.connect = saved_connect
            if anchor is not None:
                anchor.close()
            if saved_db is None:
                os.environ.pop("BROKER_DB", None)
            else:
                os.environ["BROKER_DB"] = saved_db
            get_settings.cache_clear()


def _memory_connect(uri: str):
    """db.connect's contract (Row factory, busy timeout) on the in-memory DB;
    WAL does not apply to memory databases, so that pragma is dropped."""
    def connect() -> sqlite3.Connection:
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        return conn
    return connect


def owner_and_key(capabilities: list[dict]):
    """Create the owner and one agent key holding `capabilities` as an active
    root grant (the owner set it up once, before the day starts).
    Returns (AdminContext, AuthContext)."""
    p = principals.create_owner(OWNER, OWNER_PASSWORD)
    ctx = AdminContext(p.id, p.username, "token", "simulation")
    new = auth.create_key(p.id, "sim-agent", "full", UNTHROTTLED, None)
    caps = normalize_all([from_json(c) for c in capabilities],
                         registry.get_registry().manifests())
    store.insert_root_grant(p.id, new.key_id, caps, "active", "standing grant", None,
                            p.username, decided_via="token")
    agent = auth.authenticate_bearer(f"Bearer {new.plaintext}")
    return ctx, agent

