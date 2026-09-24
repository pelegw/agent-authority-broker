"""The action queue: drafts awaiting a human and actions scheduled for later.

`create` is the single choke point for every queued action (REST, MCP, and
anything later): it inserts the row linked to its decision and fans the
notification out, so no path can queue work the owner is not told about.

Status moves only by atomic `UPDATE ... WHERE status IN (...)`: two actors
(an agent cancelling, the scheduler claiming, a Telegram tap and a console
click) can observe the same status, but only one UPDATE wins.

For agents, ownership is the authorization: a key sees and cancels only its
own rows. Cancelling only ever prevents an action, so a key whose grants
were narrowed since queuing can still undo its own work.

Rows for a disabled plugin are *held*: the TTL sweep and the scheduler skip
them, so disabling a plugin pauses its queue instead of dropping it.
"""

from __future__ import annotations

import json
import logging
import string
import time
import uuid

from .. import db, notify
from ..audit import audit
from ..errors import PolicyError
from ..logging_setup import kv
from ..plugins.registry import get_registry
from ..runtime_settings import runtime_settings

log = logging.getLogger(__name__)

# A `sending` claim older than this is a crashed delivery: fail it rather
# than leave it dangling (it may have been sent, so never back to pending).
STALE_SENDING_SECONDS = 300
_AGENT_FIELDS = ("id", "target", "action", "status", "created_at", "expires_at",
                 "decided_at", "run_at", "result")


def resolve_run_at(run_at: int | None, delay_seconds: int | None) -> int | None:
    """Validate scheduling input: a unix timestamp OR a delay, never both.
    Returns the resolved timestamp, or None for "now". Ported from WA_GW
    policy.resolve_send_at."""
    if run_at is not None and delay_seconds is not None:
        raise PolicyError(400, "pass run_at or delay_seconds, not both", "bad_request")
    now = int(time.time())
    if delay_seconds is not None:
        if delay_seconds <= 0:
            raise PolicyError(400, "delay_seconds must be positive", "bad_request")
        run_at = now + delay_seconds
    if run_at is None:
        return None
    s = runtime_settings()
    if run_at <= now + s.schedule_min_lead_seconds:
        raise PolicyError(400, f"run_at must be at least {s.schedule_min_lead_seconds}s "
                               "in the future", "bad_request")
    if run_at > now + s.schedule_max_horizon_days * 86400:
        raise PolicyError(400, f"run_at is more than {s.schedule_max_horizon_days} days out",
                          "bad_request")
    return run_at


def render_summary(template: str | None, params: dict, labels: dict[str, str]) -> str:
    """Fill a manifest summary_template. Placeholders were validated at
    manifest load (plain param names or <param>_label); missing values
    render empty rather than raising."""
    if not template:
        return ""
    values = {**{k: v for k, v in params.items()}, **labels}
    fields = [f for _, f, _, _ in string.Formatter().parse(template) if f]
    return template.format(**{f: values.get(f, "") for f in fields})


def create(auth, *, target: str, action: str, params: dict, decision_id: int,
           status: str, run_at: int | None, approval_source: str | None,
           note: str = "", resource_label: str = "", summary: str = "") -> dict:
    if status not in ("pending", "scheduled"):
        raise ValueError("a new action is pending or scheduled")
    now = int(time.time())
    expires_at = now + runtime_settings().draft_ttl_hours * 3600
    if run_at is not None:
        # A scheduled row must outlive its fire time a little, for inspection.
        expires_at = max(expires_at, run_at + 3600)
    row = {
        "id": str(uuid.uuid4()), "principal_id": auth.principal_id, "key_id": auth.key_id,
        "target": target, "action": action, "params": json.dumps(params, sort_keys=True),
        "resource_label": resource_label, "note": note[:1000], "status": status,
        "approval_source": approval_source, "created_at": now, "expires_at": expires_at,
        "decided_at": now if status == "scheduled" else None, "run_at": run_at,
        "decision_id": decision_id,
    }
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO actions (id, principal_id, key_id, target, action, params,"
            " resource_label, note, status, approval_source, created_at, expires_at,"
            " decided_at, run_at, decision_id) VALUES (:id, :principal_id, :key_id, :target,"
            " :action, :params, :resource_label, :note, :status, :approval_source,"
            " :created_at, :expires_at, :decided_at, :run_at, :decision_id)", row)
    audit(auth.name, "action.queued", row["id"],
          {"target": target, "action": action, "status": status, "run_at": run_at})
    # Never the params, the note or the resource label: the row holds them.
    log.info("action created %s", kv(action_id=row["id"], status=status, target=target,
                                     action=action, key=auth.name, run_at=run_at,
                                     decision_row=decision_id))
    if status == "pending":
        # The owner must hear about every draft; the fan-out is non-fatal.
        manifest = get_registry().manifests().get(target)
        notify.notify_action({**row, "params": params, "key_name": auth.name,
                              "summary": summary,
                              "display_name": manifest.display_name if manifest else target})
    return row


# ---- reads ---------------------------------------------------------------------

def _parse(row) -> dict:
    d = dict(row)
    for col in ("params", "result"):
        if d.get(col):
            try:
                d[col] = json.loads(d[col])
            except ValueError:
                pass
    return d


def agent_view(row: dict) -> dict:
    """Compact view for the owning agent: no params echoed back."""
    return {k: row.get(k) for k in _AGENT_FIELDS if row.get(k) is not None}


def get_row(action_id: str) -> dict | None:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT a.*, k.name AS key_name FROM actions a LEFT JOIN api_keys k"
            " ON k.id = a.key_id WHERE a.id = ?", (action_id,)).fetchone()
    return _parse(row) if row else None


def get_for_key(auth, action_id: str) -> dict:
    sweep()
    row = get_row(action_id)
    if row is None or row["key_id"] != auth.key_id:
        raise PolicyError(404, "no such action", "not_found")
    return agent_view(row)


def _page(where: str, args: list, limit: int, cursor: int | None) -> dict:
    limit = max(1, min(int(limit), 200))
    sql = ("SELECT a.rowid AS _rowid, a.*, k.name AS key_name FROM actions a"
           f" LEFT JOIN api_keys k ON k.id = a.key_id WHERE {where}")
    if cursor is not None:
        sql += " AND a.rowid < ?"
        args = [*args, cursor]
    sql += " ORDER BY a.rowid DESC LIMIT ?"
    with db.connect() as conn:
        rows = [_parse(r) for r in conn.execute(sql, [*args, limit + 1]).fetchall()]
    more = len(rows) > limit
    rows = rows[:limit]
    return {"items": rows, "next_cursor": rows[-1]["_rowid"] if more and rows else None}


def list_for_key(auth, status: str | None = None, limit: int = 50,
                 cursor: int | None = None) -> dict:
    sweep()
    where, args = "a.key_id = ?", [auth.key_id]
    if status:
        where += " AND a.status = ?"
        args.append(status)
    page = _page(where, args, limit, cursor)
    return {"items": [agent_view(r) for r in page["items"]], "next_cursor": page["next_cursor"]}


def list_all(status: str | None = None, target: str | None = None, limit: int = 100,
             cursor: int | None = None) -> dict:
    sweep()
    where, args = "1=1", []
    if status:
        where += " AND a.status = ?"
        args.append(status)
    if target:
        where += " AND a.target = ?"
        args.append(target)
    page = _page(where, args, limit, cursor)
    enabled = set(get_registry().enabled_plugins())
    for r in page["items"]:
        r.pop("_rowid", None)
        # "held" is derived, not stored: the plugin is off, the row waits.
        r["held"] = r["status"] in ("pending", "scheduled") and r["target"] not in enabled
    return page


# ---- transitions ----------------------------------------------------------------

def cancel_for_key(auth, action_id: str) -> dict:
    with db.connect() as conn:
        cur = conn.execute(
            "UPDATE actions SET status = 'canceled', decided_at = ? WHERE id = ?"
            " AND key_id = ? AND status IN ('pending', 'scheduled')",
            (int(time.time()), action_id, auth.key_id))
        if cur.rowcount == 0:
            raise PolicyError(404, "no pending or scheduled action with that id", "not_found")
    audit(auth.name, "action.canceled", action_id)
    log.info("action canceled by its key %s", kv(action_id=action_id, key=auth.name))
    return {"id": action_id, "status": "canceled"}


def sweep(now: int | None = None) -> None:
    """Lazy expiry, run before reads and decisions. Pending rows of a
    disabled plugin are held, not expired."""
    now = int(time.time()) if now is None else now
    enabled = get_registry().enabled_plugins()
    marks = ",".join("?" * len(enabled)) or "NULL"
    with db.connect() as conn:
        expired = conn.execute(
            "UPDATE actions SET status = 'expired', decided_at = ? WHERE status = 'pending'"
            f" AND expires_at < ? AND target IN ({marks})", (now, now, *enabled)).rowcount
        stale = conn.execute(
            "UPDATE actions SET status = 'failed', result = ? WHERE status = 'sending'"
            " AND decided_at < ?",
            (json.dumps({"error": "delivery interrupted; outcome unknown"}),
             now - STALE_SENDING_SECONDS)).rowcount
    if expired:
        log.info("drafts expired undecided %s", kv(count=expired))
    if stale:
        # A delivery that never finished: it may have happened. Never retried.
        log.warning("interrupted deliveries marked failed (outcome unknown) %s",
                    kv(count=stale))
