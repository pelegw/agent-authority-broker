"""Append-only ops audit trail: logins, key changes, plugin toggles.

This is for humans reconstructing what happened on the admin plane. It is
never load-bearing: policy decisions live in the hash-chained `decisions`
table and budgets in `capacity_ledger`, so nothing here is read to decide
anything.
"""

import json
import time

from . import db


def audit(actor: str, action: str, resource: str = "",
          detail: dict | None = None, result: str = "ok", *,
          actor_principal: str | None = None,
          actor_via: str | None = None) -> None:
    """Write one audit row.

    `actor` is the display name (key name or owner username). When a human
    acted, pass `actor_principal` (principal id) and `actor_via`
    (session | token | telegram) so the row is attributable, not just "admin".
    """
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO audit_log (ts, actor, action, resource, detail, result,"
            " actor_principal, actor_via) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (int(time.time()), actor, action, resource,
             json.dumps(detail or {}), result, actor_principal, actor_via),
        )
