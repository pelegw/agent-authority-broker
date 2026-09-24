"""A seeded synthetic agent workload: what a busy agent does in a working day.

The mix is fixed by the brief (§11.5): ~300 actions an hour, 70% reads, 25%
writes to resources the agent already works in, 5% writes that reach
outside them. The workload is generated once, up front, and replayed
unchanged through each approval model, so any difference in interrupts is
the model's doing and never the dice's.

"Outside" is a finite set of rooms (`new_pool`), not an endless stream of
fresh ones: a real agent's world is bounded, so the second write to a room
it expanded into is ordinary work. That is exactly what makes standing
grants pay off over time, and it is why the number of distinct expansion
rooms, not the number of expansion writes, is the expected interrupt count.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

READ, WRITE_KNOWN, WRITE_NEW = "read", "write_known", "write_new"


@dataclass(frozen=True)
class Event:
    hour: int          # simulated working hour, 0-based (for per-hour stats)
    kind: str          # read | write_known | write_new
    room: str


@dataclass(frozen=True)
class Workload:
    events: tuple[Event, ...]
    known_rooms: tuple[str, ...]     # what the standing grant covers up front
    new_rooms: tuple[str, ...]       # rooms the agent may expand into

    @property
    def writes(self) -> int:
        return sum(e.kind != READ for e in self.events)

    @property
    def distinct_new(self) -> int:
        return len({e.room for e in self.events if e.kind == WRITE_NEW})


def rooms(first: int, count: int) -> tuple[str, ...]:
    """Echo room ids r<first>..r<first+count-1> (echo normalizes `r<digits>`)."""
    return tuple(f"r{i}" for i in range(first, first + count))


def generate(hours: int, seed: int, per_hour: int = 300, known: int = 20,
             new_pool: int = 20, mix: tuple[float, float, float] = (0.70, 0.25, 0.05)
             ) -> Workload:
    """Deterministic for a given argument tuple: one `random.Random(seed)`
    drives every choice, and nothing else is consulted."""
    if hours < 1 or per_hour < 1 or known < 1 or new_pool < 1:
        raise ValueError("hours, per_hour, known and new_pool must be positive")
    rng = random.Random(seed)
    known_rooms, new_rooms = rooms(1, known), rooms(known + 1, new_pool)
    events = []
    for hour in range(hours):
        for _ in range(per_hour):
            kind = rng.choices((READ, WRITE_KNOWN, WRITE_NEW), weights=mix)[0]
            # Reads roam everywhere (the read grant is on `*`), so they draw
            # from both sets; writes stay in their own set.
            pool = {READ: known_rooms + new_rooms, WRITE_KNOWN: known_rooms,
                    WRITE_NEW: new_rooms}[kind]
            events.append(Event(hour, kind, rng.choice(pool)))
    return Workload(tuple(events), known_rooms, new_rooms)
