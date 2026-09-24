"""Deliver a queued action: atomic claim, re-check, then the engine's execute.

Ported from WA_GW admin_services `_claim` / `_conflict` / `_deliver_claimed`.

Claim first, deliver second: a row moves `pending|scheduled -> sending` with
one `UPDATE ... WHERE status = ?`, so a double tap, a console click racing a
Telegram tap, or a scheduler tick racing a cancel can never deliver twice.
FastAPI runs sync routes on a threadpool, so the race is real even with one
worker.

Re-check at delivery, because time has passed since queuing:
  * every row needs its key alive (and every ancestor key) and its plugin
    enabled; a disabled plugin *holds* the row (released, not dropped);
  * a row no human approved (`approval_source = automatic`, i.e. a scheduled
    direct call) re-runs `policy.evaluate` and needs `allow` now;
  * a human-approved row needs the resource not hidden/denied; the human's
    decision stands in for grant coverage (the plan: key alive, plugin
    enabled, resource not hidden).
Each delivery appends its own decision row (actor recorded for humans).

Failures: 503 or 429 release the row back to `release_status` (pending when
a human approved it now, scheduled for the scheduler) so it is retried; a
502 marks it `failed` with the result recorded and it is NEVER retried
automatically, since the action may have happened.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import replace

from .. import db, engine
from ..audit import audit
from ..auth import context_for_key
from ..errors import PolicyError
from ..hidden import deny_sets, is_denied
from ..logging_setup import kv
from ..plugins.adapter import CallScope
from ..plugins.registry import get_registry
from ..policy import NOT_FOUND, Decision, evaluate
from . import queue

log = logging.getLogger(__name__)


def claim(action_id: str, new_status: str, now: int, from_status: str = "pending", *,
          ctx=None) -> bool:
    """Atomically move a row from `from_status` to `new_status`. When `ctx`
    (an AdminContext) is given, the human decision is recorded on the row."""
    human = ctx is not None
    with db.connect() as conn:
        cur = conn.execute(
            "UPDATE actions SET status = ?, decided_at = ?,"
            " approval_source = CASE WHEN ? THEN 'human' ELSE approval_source END,"
            " decided_by_principal = CASE WHEN ? THEN ? ELSE decided_by_principal END,"
            " decided_via = CASE WHEN ? THEN ? ELSE decided_via END"
            " WHERE id = ? AND status = ?",
            (new_status, now, human, human, ctx.username if human else None,
             human, ctx.via if human else None, action_id, from_status))
        return cur.rowcount == 1


def conflict(action_id: str, expected: str = "pending") -> PolicyError:
    row = queue.get_row(action_id)
    if row is None:
        return PolicyError(404, "no such action", "not_found")
    return PolicyError(409, f"action is {row['status']!r}, not {expected}", "conflict")


def _set(action_id: str, status: str, result: dict | None = None) -> None:
    with db.connect() as conn:
        conn.execute("UPDATE actions SET status = ?, decided_at = ?,"
                     " result = COALESCE(?, result) WHERE id = ?",
                     (status, int(time.time()),
                      json.dumps(result, sort_keys=True) if result is not None else None,
                      action_id))


def _original_chain(decision_id: int | None) -> tuple[str, ...]:
    if decision_id is None:
        return ()
    with db.connect() as conn:
        row = conn.execute("SELECT grant_chain FROM decisions WHERE id = ?",
                           (decision_id,)).fetchone()
    try:
        return tuple(json.loads(row["grant_chain"])) if row else ()
    except ValueError:
        return ()


def _human_decision(key_ctx, row: dict, params: dict, now: int, request_id: str) -> Decision:
    d = evaluate(key_ctx, row["target"], row["action"], params, now, as_draft=True,
                 request_id=request_id)
    if d.decision != "deny":
        return replace(d, decision="allow", status=200, reason="human_approved")
    if d.status != 403:
        return d          # hidden (404), unavailable (503), invalid (400): no delivery
    # Out of grant now, but a human approved this exact action: deliver with
    # the deny sets still applied, charged to the chain that queued it. The
    # resource must still not be hidden (policy only checks that once a
    # capability covers the call, which none does here).
    deny = deny_sets(key_ctx, row["target"])
    reg = get_registry()
    act = reg.manifests()[row["target"]].action(row["action"])
    if d.resource and act.resource and is_denied(deny.get(act.resource, set()), d.resource,
                                                 act.resource, reg.ancestors):
        return Decision("deny", 404, "hidden", NOT_FOUND, "not_found", resource=d.resource,
                        params=d.params, side_effect=d.side_effect)
    vis = {k: {"deny": sorted(v), "allow_only": None} for k, v in deny.items() if v}
    return Decision("allow", 200, "human_approved", resource=d.resource, params=d.params,
                    side_effect=d.side_effect, grant_chain_ids=_original_chain(row["decision_id"]),
                    enforced_where=d.enforced_where,
                    scope=CallScope(request_id=request_id, visibility=vis))


def deliver_claimed(action_id: str, row: dict, release_status: str, actor: str,
                    ctx=None) -> dict:
    """Deliver a row already claimed into `sending`."""
    now = int(time.time())
    target, action = row["target"], row["action"]
    params = row["params"] if isinstance(row["params"], dict) else json.loads(row["params"])
    key_ctx = context_for_key(row["key_id"])
    if key_ctx is None:
        _set(action_id, "canceled", {"error": "key disabled or expired"})
        audit(actor, "action.denied", action_id, {"reason": "key disabled or expired"},
              result="denied")
        log.info("action canceled at delivery: its key is disabled or expired %s",
                 kv(action_id=action_id, target=target, action=action))
        raise PolicyError(403, "the action's key is disabled or expired", "forbidden")
    if not get_registry().is_enabled(target):
        _set(action_id, release_status)          # held, not dropped
        log.info("action held: plugin disabled %s",
                 kv(action_id=action_id, target=target, action=action, status=release_status))
        raise PolicyError(409, "plugin is disabled; the action is held", "held")

    request_id = engine.new_request_id()
    if row.get("approval_source") == "human":
        d = _human_decision(key_ctx, row, params, now, request_id)
    else:
        d = evaluate(key_ctx, target, action, params, now, request_id=request_id)
        if d.decision == "draft":
            d = Decision("deny", 403, "no_longer_direct",
                         "queued authorization no longer allows direct delivery",
                         "out_of_grant", resource=d.resource, params=d.params,
                         side_effect=d.side_effect)
    engine.record_decision(key_ctx, target, action, params, d, request_id,
                           actor_principal=ctx.username if ctx else None,
                           actor_via=ctx.via if ctx else None)
    if d.decision == "deny":
        if d.status == 503:
            _set(action_id, release_status)
        else:
            _set(action_id, "canceled", {"error": d.message or d.reason})
        audit(actor, "action.denied", action_id, {"reason": d.reason}, result="denied")
        log.info("action not delivered: denied at re-check %s", kv(
            action_id=action_id, reason=d.reason, status=d.status,
            row_status=release_status if d.status == 503 else "canceled"))
        raise engine.deny_error(d)

    try:
        result = engine.execute(key_ctx, d, target, action, request_id,
                                actor_principal=ctx.username if ctx else None,
                                actor_via=ctx.via if ctx else None)
    except PolicyError as exc:
        if exc.status in (429, 503):
            _set(action_id, release_status)      # transient: retry later
            audit(actor, "action.deferred", action_id, {"status": exc.status}, result="error")
            log.info("action deferred; will retry %s", kv(
                action_id=action_id, status=exc.status, row_status=release_status))
        else:
            _set(action_id, "failed", {"error": str(exc), "status": exc.status})
            audit(actor, "action.failed", action_id, {"status": exc.status}, result="error")
            log.warning("action failed %s", kv(action_id=action_id, status=exc.status,
                                               code=exc.code))
        raise
    stored = ({"binary": True, "mime": result.mime,
               "b64": base64.b64encode(result.binary).decode()} if result.binary is not None
              else {"data": result.data})
    _set(action_id, "done", stored)
    audit(actor, "action.done", action_id, {"target": target, "action": action,
                                            "on_behalf_of": key_ctx.name})
    log.info("action delivered %s", kv(action_id=action_id, target=target, action=action,
                                       key=key_ctx.name, by=actor))
    return {"id": action_id, "status": "done", "result": stored}
