"""The ops audit log records who acted, and through which surface."""

import json

from broker import db
from broker.audit import audit


def test_audit_records_actor_principal_and_via(env):
    audit("owner", "plugin.enable", "whatsapp", {"why": "test"},
          actor_principal="p-1", actor_via="session")
    audit("reader", "key.used")
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
    assert rows[0]["actor"] == "owner"
    assert rows[0]["actor_principal"] == "p-1"
    assert rows[0]["actor_via"] == "session"
    assert json.loads(rows[0]["detail"]) == {"why": "test"}
    assert rows[1]["actor_principal"] is None and rows[1]["result"] == "ok"
