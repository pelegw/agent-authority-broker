"""The two approval models, and the simulated agent + owner that drive them.

Both replay one workload through `engine.perform`, the same path REST and MCP
use, so every decision, draft, ledger charge and grant is the real thing.
They differ only in the authority the key starts with:

  standing    reads direct on `*`; writes direct on the known rooms. A write
              outside them is denied `out_of_grant`, the agent asks once
              (`request_permission` for that room), the owner approves, and
              the write is retried; every later write there is direct.
  per_action  reads the same; writes draft-mode on every room, so each
              write is a pending action the owner must approve.

The simulated owner approves everything at once: this measures how often a
human is interrupted, not how long they take (docs/approval-volume.md).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from broker import engine
from broker.errors import PolicyError
from broker.services import admin, agent

from . import harness
from .workload import READ, Workload

TARGET = "echo"
STANDING, PER_ACTION = "standing", "per_action"


@dataclass
class Tally:
    """What one model cost over the workload. `interrupts` is every time a
    human had to look at something: pending actions plus grant requests."""
    model: str
    hours: int
    actions: int = 0
    reads: int = 0
    writes: int = 0
    performed: int = 0            # calls that reached the plugin successfully
    pending_approvals: int = 0
    grant_requests: int = 0
    denies: int = 0               # deny decisions returned to the agent (403/404)
    budget_429: int = 0
    hourly_interrupts: list[int] = field(default_factory=list)

    @property
    def interrupts(self) -> int:
        return self.pending_approvals + self.grant_requests

    @property
    def interrupts_per_hour(self) -> float:
        return round(self.interrupts / self.hours, 2)

    def as_dict(self) -> dict:
        return {"model": self.model, "actions": self.actions, "reads": self.reads,
                "writes": self.writes, "performed": self.performed,
                "pending_approvals": self.pending_approvals,
                "grant_requests": self.grant_requests, "interrupts": self.interrupts,
                "interrupts_per_hour": self.interrupts_per_hour, "denies": self.denies,
                "budget_429": self.budget_429, "hourly_interrupts": self.hourly_interrupts}


def capabilities(model: str, wl: Workload, per_day: int) -> list[dict]:
    reads = {"target": TARGET, "actions": ["list_items"]}
    if model == STANDING:
        writes = {"target": TARGET, "actions": ["post_item"],
                  "selector": {"room": list(wl.known_rooms)}, "budget": {"per_day": per_day}}
    elif model == PER_ACTION:
        writes = {"target": TARGET, "actions": ["post_item"], "mode": "draft",
                  "budget": {"per_day": per_day}}
    else:
        raise ValueError(f"unknown model {model!r}")
    return [reads, writes]


def run(model: str, wl: Workload, per_day: int, on_disk: bool = False) -> Tally:
    """Replay `wl` under `model` in a fresh sandboxed broker."""
    hours = max(e.hour for e in wl.events) + 1
    tally = Tally(model, hours, hourly_interrupts=[0] * hours)
    with harness.sandbox(on_disk):
        owner, key = harness.owner_and_key(capabilities(model, wl, per_day))
        sim = _Day(tally, owner, key, per_day)
        for n, event in enumerate(wl.events):
            sim.step(n, event)
    return tally


class _Day:
    """One working day: the agent acts, the owner answers every interrupt."""

    def __init__(self, tally: Tally, owner, key, per_day: int):
        self.t, self.owner, self.key, self.per_day = tally, owner, key, per_day

    def step(self, n: int, event) -> None:
        self.t.actions += 1
        if event.kind == READ:
            self.t.reads += 1
            self._call(event.hour, "list_items", {"room": event.room, "limit": 5})
        else:
            self.t.writes += 1
            self._write(event.hour, event.room, f"sim write {n}")

    def _write(self, hour: int, room: str, text: str) -> None:
        params = {"room": room, "text": text}
        denied = self._call(hour, "post_item", params)
        if denied is None or denied.code != "out_of_grant":
            return
        # The agent's cue (hint: request_permission): ask once for exactly
        # this room, bounded like the rest of its write authority. A standing
        # grant is a request to act directly, so it says so: an agent request
        # without a mode asks for draft (every write would still interrupt).
        req = agent.request_permission(
            self.key, [{"target": TARGET, "actions": ["post_item"], "mode": "direct",
                        "selector": {"room": [room]}, "budget": {"per_day": self.per_day}}],
            reason=f"need to post in {room}")
        self._interrupt(hour, "grant")
        admin.decide_grant(self.owner, req["id"], "active")
        self._call(hour, "post_item", params)

    def _call(self, hour: int, action: str, params: dict) -> PolicyError | None:
        """Perform one call; returns the deny it ended in, if any."""
        try:
            r = engine.perform(self.key, TARGET, action, params)
        except PolicyError as exc:
            return self._failed(exc)
        if r.status == 202:
            self._interrupt(hour, "action")
            try:
                admin.approve_action(self.owner, r.body["action_id"])
            except PolicyError as exc:
                return self._failed(exc)
        self.t.performed += 1
        return None

    def _failed(self, exc: PolicyError) -> PolicyError:
        if exc.code == "budget_exhausted":
            self.t.budget_429 += 1
        elif exc.status in (403, 404):
            self.t.denies += 1
        else:
            # Nothing else is expected from this workload; fail loudly rather
            # than let an unexplained error skew the counts.
            raise exc
        return exc

    def _interrupt(self, hour: int, kind: str) -> None:
        if kind == "grant":
            self.t.grant_requests += 1
        else:
            self.t.pending_approvals += 1
        self.t.hourly_interrupts[hour] += 1
