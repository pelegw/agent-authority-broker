"""`perform()`: the one dispatch path every agent surface goes through.

REST (routers/targets.py) and MCP (next lane) both call `perform`, so policy,
recording, budgets and error mapping can never diverge between surfaces.

  1. scheduling input is validated (400, nothing else happens);
  2. `policy.evaluate` decides;
  3. a `decision` row is appended to the hash-chained record BEFORE any side
     effect, for denies too;
  4. deny: raise. Draft, or an allowed call with `run_at`: the action queue
     (`actions.queue.create`, which notifies) and a 202;
  5. allowed now: writes reserve budget (reads skip the ledger), the plugin
     performs, an `outcome` row follows, the reservation is committed or
     released (503 and 4xx release, 502 keeps it for 24 h);
  6. any result row carrying `resource_ref` is filtered again against the
     call's visibility, in case a plugin forgot (belt and braces).

`execute` is steps 5 and 6 alone; actions/deliver.py reuses it for queued
actions after its own re-check.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

from . import decisions, ledger
from .actions import queue
from .errors import PolicyError
from .hidden import is_denied
from .plugins.adapter import AdapterError, CallScope, Result
from .plugins.registry import get_registry
from .policy import NOT_FOUND, Decision, evaluate

_ADAPTER_ERRORS = {
    503: ("target unavailable; not performed, safe to retry", "unavailable"),
    502: ("outcome unknown; the action may have happened, do not retry blindly",
          "unknown_outcome"),
}
_WALK_DEPTH = 3


@dataclass(frozen=True)
class EngineResult:
    status: int                   # 200 or 202
    body: Any = None              # JSON body (data, or the 202 envelope)
    binary: bytes | None = None
    mime: str | None = None


def new_request_id() -> str:
    return uuid.uuid4().hex


def record_decision(auth, target: str, action: str, params: Any, d: Decision,
                    request_id: str, *, actor_principal: str | None = None,
                    actor_via: str | None = None) -> int:
    return decisions.record(
        request_id=request_id, kind="decision", auth=auth, target=target, action=action,
        grant_chain=d.grant_chain_ids, resource=d.resource,
        params=d.params if d.params else params, decision=d.decision, reason=d.reason,
        enforced_where=d.enforced_where, actor_principal=actor_principal, actor_via=actor_via)


def deny_error(d: Decision) -> PolicyError:
    return PolicyError(d.status, d.message or d.reason, d.code or None, hint=d.hint)


def perform(auth, target: str, action: str, params: Any, *, as_draft: bool = False,
            run_at: int | None = None, delay_seconds: int | None = None,
            note: str = "") -> EngineResult:
    when = queue.resolve_run_at(run_at, delay_seconds)
    request_id = new_request_id()
    d = evaluate(auth, target, action, params, int(time.time()), as_draft=as_draft,
                 scheduled=when is not None, request_id=request_id)
    decision_id = record_decision(auth, target, action, params, d, request_id)
    if d.decision == "deny":
        raise deny_error(d)
    if d.decision == "draft" or when is not None:
        return _enqueue(auth, target, action, d, decision_id, when, note)
    result = execute(auth, d, target, action, request_id)
    if result.binary is not None:
        return EngineResult(200, binary=result.binary, mime=result.mime)
    return EngineResult(200, body=result.data)


def _enqueue(auth, target: str, action: str, d: Decision, decision_id: int,
             when: int | None, note: str) -> EngineResult:
    reg = get_registry()
    manifest = reg.manifests()[target]
    act = manifest.action(action)
    labels, resource_label = {}, ""
    if act.selector_param and d.resource and act.resource:
        try:
            resource_label = reg.adapter(target).label(act.resource, [d.resource]).get(
                d.resource, "")
        except AdapterError:
            resource_label = ""           # a label is cosmetic; never block on it
        labels[f"{act.selector_param}_label"] = resource_label or d.resource
    draft = d.decision == "draft"
    row = queue.create(
        auth, target=target, action=action, params=d.params, decision_id=decision_id,
        status="pending" if draft else "scheduled", run_at=when,
        approval_source=None if draft else "automatic", note=note,
        resource_label=resource_label or d.resource,
        summary=queue.render_summary(act.summary_template, d.params, labels))
    return EngineResult(202, body={"status": "pending_approval" if draft else "scheduled",
                                   "action_id": row["id"]})


def execute(auth, d: Decision, target: str, action: str, request_id: str, *,
            actor_principal: str | None = None, actor_via: str | None = None) -> Result:
    """Perform an allowed decision: ledger, plugin, outcome row, post-filter."""
    reg = get_registry()
    adapter = reg.adapter(target)
    scope = d.scope or CallScope(request_id=request_id)

    def outcome(value: str) -> None:
        decisions.record(request_id=request_id, kind="outcome", auth=auth, target=target,
                         action=action, grant_chain=d.grant_chain_ids, resource=d.resource,
                         p_hash=decisions.params_hash(d.params), outcome=value,
                         actor_principal=actor_principal, actor_via=actor_via)

    if adapter is None:
        outcome("error:404")
        raise PolicyError(404, "no such target", "not_found")
    reservation = None
    if d.side_effect != "read":
        try:
            reservation = ledger.reserve(auth, d.grant_chain_ids, target, action)
        except PolicyError as exc:
            outcome(exc.code)
            raise
    try:
        result = adapter.perform(action, d.params, scope)
    except AdapterError as exc:
        if reservation is not None and exc.status != 502:
            ledger.release(reservation)   # definitely not performed
        outcome({502: "unknown", 503: "unavailable"}.get(exc.status, f"error:{exc.status}"))
        raise adapter_error(exc) from exc
    if reservation is not None:
        ledger.commit(reservation)
    try:
        result = post_filter(result, scope, reg.ancestors)
    except PolicyError:
        outcome("filtered")
        raise
    outcome("ok")
    return result


def adapter_error(exc: AdapterError) -> PolicyError:
    """Map a plugin failure to the agent-facing error. Every 404 gets the one
    NOT_FOUND body, so a hidden resource and a missing one look the same."""
    if exc.status == 404:
        return PolicyError(404, NOT_FOUND, "not_found")
    if exc.status in _ADAPTER_ERRORS:
        msg, code = _ADAPTER_ERRORS[exc.status]
        return PolicyError(exc.status, msg, code)
    return PolicyError(exc.status, exc.message)


# ---- the engine's own visibility filter ---------------------------------------

def _hidden_ref(ref: Any, scope: CallScope, ancestors) -> bool:
    if not isinstance(ref, dict):
        return False
    kind, rid = ref.get("kind"), ref.get("id")
    if not isinstance(kind, str) or not isinstance(rid, str):
        return True                        # a malformed ref is not trusted
    vis = scope.visibility.get(kind)
    if not vis:
        return False
    if is_denied(set(vis.get("deny") or ()), rid, kind, ancestors):
        return True
    allow = vis.get("allow_only")
    if allow is not None:
        allow = set(allow)
        return rid not in allow and not any(a in allow for a in ancestors(kind, rid))
    return False


def _filter(value: Any, scope: CallScope, ancestors, depth: int) -> Any:
    if depth > _WALK_DEPTH:
        return value
    if isinstance(value, list):
        return [_filter(v, scope, ancestors, depth + 1) for v in value
                if not (isinstance(v, dict) and _hidden_ref(v.get("resource_ref"), scope,
                                                            ancestors))]
    if isinstance(value, dict):
        return {k: _filter(v, scope, ancestors, depth + 1) for k, v in value.items()}
    return value


def post_filter(result: Result, scope: CallScope, ancestors) -> Result:
    """Drop every row whose `resource_ref` the call may not see. A single
    object that is itself hidden becomes a 404."""
    if result.binary is not None or result.data is None:
        return result
    data = result.data
    if isinstance(data, dict) and _hidden_ref(data.get("resource_ref"), scope, ancestors):
        raise PolicyError(404, NOT_FOUND, "not_found")
    return Result(data=_filter(data, scope, ancestors, 0))


# ---- long-poll reads (REST ?wait=) -----------------------------------------------

@dataclass(frozen=True)
class Poll:
    request_id: str
    target: str
    action: str
    params: dict


def open_poll(auth, target: str, action: str, params: Any) -> Poll:
    """Evaluate and record once for a whole long-poll; each step re-checks
    silently so a revocation mid-wait still ends it."""
    manifest = get_registry().manifests().get(target)
    act = manifest.action(action) if manifest else None
    request_id = new_request_id()
    d = evaluate(auth, target, action, params, int(time.time()), request_id=request_id)
    if d.decision != "deny" and not (act and act.long_poll):
        d = Decision("deny", 400, "not_long_poll", "this action does not long-poll",
                     "bad_request", params=d.params, side_effect=d.side_effect)
    record_decision(auth, target, action, params, d, request_id)
    if d.decision == "deny":
        raise deny_error(d)
    return Poll(request_id, target, action, d.params)


def poll_step(auth, poll: Poll) -> Any:
    d = evaluate(auth, poll.target, poll.action, poll.params, int(time.time()),
                 request_id=poll.request_id)
    if d.decision != "allow":
        close_poll(auth, poll, "revoked")
        raise deny_error(d) if d.decision == "deny" else PolicyError(403, "no longer allowed")
    adapter = get_registry().adapter(poll.target)
    try:
        result = adapter.perform(poll.action, d.params, d.scope)
    except AdapterError as exc:
        close_poll(auth, poll, f"error:{exc.status}")
        raise adapter_error(exc) from exc
    return post_filter(result, d.scope, get_registry().ancestors).data


def close_poll(auth, poll: Poll, outcome: str = "ok") -> None:
    decisions.record(request_id=poll.request_id, kind="outcome", auth=auth,
                     target=poll.target, action=poll.action,
                     p_hash=decisions.params_hash(poll.params), outcome=outcome)


def is_empty(data: Any) -> bool:
    """Nothing new: falsy, or a dict whose every list is empty (the shape
    long-poll actions return, e.g. {"cursor": 7, "items": []})."""
    if not data:
        return True
    if isinstance(data, dict):
        lists = [v for v in data.values() if isinstance(v, list)]
        return bool(lists) and all(not v for v in lists)
    return False
