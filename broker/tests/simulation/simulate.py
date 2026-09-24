"""Approval-volume simulation: how often is a human interrupted?

Runs one seeded synthetic workload (workload.py) through the real engine
under a standing-grant model and a per-action-approval model (models.py),
then prints a comparison table and a JSON summary. Same seed, same output.

    cd broker && python -m tests.simulation.simulate --hours 8 [--seed 7] [--json]
    aab simulate --hours 8                      # the same, from the CLI

The brief's design constraint (§6.3) is that human interrupts are reserved
for scope expansion; this measures it: under standing grants the interrupt
count should equal the number of distinct rooms the agent expanded into.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import models
from .workload import generate

DEFAULT_SEED = 7
DEFAULT_PER_DAY = 1000


def simulate(hours: int = 8, seed: int = DEFAULT_SEED, per_hour: int = 300,
             per_day: int = DEFAULT_PER_DAY, known: int = 20, new_pool: int = 20,
             on_disk: bool = False) -> dict:
    """Run both models over one workload; returns the JSON-able summary."""
    wl = generate(hours, seed, per_hour=per_hour, known=known, new_pool=new_pool)
    standing = models.run(models.STANDING, wl, per_day, on_disk)
    per_action = models.run(models.PER_ACTION, wl, per_day, on_disk)
    ratio = standing.interrupts / per_action.interrupts if per_action.interrupts else 0.0
    return {
        "seed": seed, "hours": hours, "actions_per_hour": per_hour,
        "known_rooms": known, "new_room_pool": new_pool, "per_day_budget": per_day,
        "workload": {"actions": len(wl.events), "writes": wl.writes,
                     "writes_to_new_rooms": sum(e.kind == "write_new" for e in wl.events),
                     "distinct_new_rooms": wl.distinct_new},
        "models": {t.model: t.as_dict() for t in (standing, per_action)},
        "interrupt_ratio": round(ratio, 4),
    }


_ROWS = [("actions", "actions"), ("reads", "reads"), ("writes", "writes"),
         ("performed", "performed"), ("pending approvals", "pending_approvals"),
         ("grant requests", "grant_requests"), ("human interrupts", "interrupts"),
         ("interrupts / hour", "interrupts_per_hour"), ("denies", "denies"),
         ("budget 429s", "budget_429")]


def table(summary: dict) -> str:
    """A fixed-width comparison; the per-hour row shows the standing model's
    interrupts tapering off as its grant grows."""
    a, b = summary["models"]["standing"], summary["models"]["per_action"]
    w = summary["workload"]
    lines = [f"approval volume: {summary['hours']} h x {summary['actions_per_hour']} actions/h, "
             f"seed {summary['seed']}, per_day budget {summary['per_day_budget']}",
             f"distinct new rooms touched: {w['distinct_new_rooms']} "
             f"({w['writes_to_new_rooms']} writes to them)", "",
             f"{'':<20}{'standing grant':>16}{'per-action':>14}",
             "-" * 50]
    lines += [f"{label:<20}{a[k]!s:>16}{b[k]!s:>14}" for label, k in _ROWS]
    lines += ["-" * 50,
              f"standing / per-action interrupts: {summary['interrupt_ratio']:.1%}", "",
              "interrupts by hour:",
              "  standing   " + " ".join(f"{n:>4}" for n in a["hourly_interrupts"]),
              "  per-action " + " ".join(f"{n:>4}" for n in b["hourly_interrupts"])]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Compare human interrupts under standing "
                                            "grants vs per-action approval.")
    add_arguments(p)
    return p


def add_arguments(p: argparse.ArgumentParser) -> None:
    """Shared with `aab simulate` so the two entry points cannot drift."""
    p.add_argument("--hours", type=int, default=8)
    p.add_argument("--seed", type=int, default=DEFAULT_SEED)
    p.add_argument("--per-hour", type=int, default=300, help="actions per hour")
    p.add_argument("--per-day", type=int, default=DEFAULT_PER_DAY,
                   help="per_day budget on the write authority")
    p.add_argument("--new-pool", type=int, default=20,
                   help="rooms outside the standing grant the agent may expand into")
    p.add_argument("--on-disk", action="store_true",
                   help="use a temp-file database instead of in-memory (slower)")
    p.add_argument("--json", action="store_true", help="print only the JSON summary")


def run_from_args(args) -> int:
    summary = simulate(hours=args.hours, seed=args.seed, per_hour=args.per_hour,
                       per_day=args.per_day, new_pool=args.new_pool, on_disk=args.on_disk)
    if not args.json:
        print(table(summary))
        print()
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    return run_from_args(build_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
