# Approval volume: standing grants vs per-action approval

Human-in-the-loop control fails when there is too much of it: the approver
starts rubber-stamping, and the approvals become a record of oversight that
never actually happened (brief §6.3). The broker's answer is **standing
narrow grants**. The agent asks once for a bounded capability, and a human
is interrupted only when the agent needs more scope. This simulation
measures how many interrupts that design produces.

## Running it

```bash
cd broker
python -m tests.simulation.simulate --hours 8            # table + JSON summary
python -m tests.simulation.simulate --hours 8 --json     # JSON only
aab simulate --hours 8 --seed 7                          # same thing, from a source checkout
```

Other flags (module form only): `--per-hour`, `--per-day` (the write
budget, default 1000), `--new-pool`, and `--on-disk`. The output is deterministic for a
given seed. The test `tests/simulation/test_approval_volume.py` runs a
3-hour workload in the normal suite.

## What it models

**Workload** (`tests/simulation/workload.py`). A seeded synthetic day of
300 actions an hour on the in-process `echo` plugin:

| Share | Action | Resource |
|---|---|---|
| 70% | `list_items` (read) | any room: the 20 known rooms and the 20 expansion rooms |
| 25% | `post_item` (write) | one of the 20 **known** rooms, `r1`–`r20` |
| 5% | `post_item` (write) | one of 20 **expansion** rooms, `r21`–`r40` |

The expansion rooms are a finite pool, so the agent's world is bounded. The
first write to a given expansion room is scope expansion. Later writes to
that room are ordinary work. The workload is generated once and replayed
unchanged through both models.

**Model A: standing grant.** The key starts with one root grant: reads
direct on `*`, and writes direct on the 20 known rooms with
`budget.per_day` = 1000 (the default; `--per-day` changes it). A write
to an expansion room gets a 403
`out_of_grant` (hint `request_permission`). The agent then asks for
`post_item` on exactly that room, with the same budget and an explicit
`"mode": "direct"` (an agent request without a mode asks for draft). The
owner approves through `decide_grant`, and the agent retries. From then on,
writes to that room are direct.

**Model B: per-action.** The reads are the same. Writes use draft mode on
every room, so each write goes into the queue as a pending action, and the
owner approves it through `approve_action`.

Everything runs through `engine.perform` and the real admin services:
policy evaluation, the hash-chained decision record, drafts and the action
queue, the capacity ledger, and grant requests. The owner is simulated and
approves every request immediately.

**Counted:** human interrupts (pending actions plus grant requests),
interrupts per hour, denies returned to the agent, and budget 429s.

## Results (8 hours, seed 7)

Run on Windows 11 with Python 3.14.6 (AMD64), with the default `per_day`
budget of 1000. The run took about 18 s.

```
approval volume: 8 h x 300 actions/h, seed 7, per_day budget 1000
distinct new rooms touched: 20 (120 writes to them)

                      standing grant    per-action
--------------------------------------------------
actions                         2400          2400
reads                           1688          1688
writes                           712           712
performed                       2400          2400
pending approvals                  0           712
grant requests                    20             0
human interrupts                  20           712
interrupts / hour                2.5          89.0
denies                            20             0
budget 429s                        0             0
--------------------------------------------------
standing / per-action interrupts: 2.8%

interrupts by hour:
  standing     14    2    3    0    1    0    0    0
  per-action   90   78   85   91   79   96   98   95
```

What the numbers show:

- **Standing-grant interrupts equal the number of distinct expansion rooms**
  (20). The only denies are the 20 `out_of_grant` responses that prompted
  each request.
- **The standing-grant cost is front-loaded.** 14 of the 20 interrupts come
  in the first hour, while the agent discovers its world. After the third
  hour there is only one more. Per-action approval costs about 90
  interrupts every hour, indefinitely, which is the rubber-stamping regime.
- **Over one hour, the ratio does not meet the 10% target.** Across seeds
  1–8, the one-hour ratio ranges from 7.9% to 15.6%. Over 2 hours it is
  7.0–9.9%, and over 3 hours it is 6.0–7.5%. The advantage builds up over
  time. It is not there from the first minute. For that reason the suite
  checks the claim over 3 hours.

### What budget exhaustion looks like

The same run with `--per-day 200`. The implementation plan used 200 as an
illustration, but it is too small for this workload:

```
approval volume: 8 h x 300 actions/h, seed 7, per_day budget 200
distinct new rooms touched: 20 (120 writes to them)

                      standing grant    per-action
--------------------------------------------------
actions                         2400          2400
reads                           1688          1688
writes                           712           712
performed                       2008          1888
pending approvals                  0           712
grant requests                    20             0
human interrupts                  20           712
interrupts / hour                2.5          89.0
denies                            20             0
budget 429s                      392           512
--------------------------------------------------
standing / per-action interrupts: 2.8%

interrupts by hour:
  standing     14    2    3    0    1    0    0    0
  per-action   90   78   85   91   79   96   98   95
```

- **The interrupt counts do not change.** An interrupt is counted when a
  human is asked, whether or not the action is delivered afterwards.
- **Model A runs out of budget at about 2 h 40 min.** The workload makes
  about 75 known-room writes an hour against the root grant's 200 a day.
  After that, 392 known-room writes fail with 429. Writes to expansion
  rooms keep working, because each expansion grant has a budget of its
  own (see the design observation below).
- **In model B, approvals are wasted.** The owner approves 512 actions that
  then fail with 429 at delivery, and each of those actions goes back to
  `pending`. Those approvals interrupted the owner and accomplished
  nothing. Neither model counts a request for more budget as an
  interrupt (see below).

### Design observation: capacity grows with approvals

Budgets belong to grants, not to keys. Each approved expansion grant brings
its own `per_day` budget, and the ledger charges a write only to the grant
chain that authorized it. After model A's 20 expansions, the key's total
daily write capacity is 21 × `per_day`: the root grant plus one budget per
expansion room. A human approved every one of those grants, so no
authority was gained without consent. But an approver reads a request as
"may post in room r27", not as "may make another 1000 writes a day", and
nothing on the request says so. Two possible responses are showing the
budget a request carries on its approval card, or adding a key-level
ceiling that caps the key's daily writes no matter how many grants it
holds.

## What it does not model

- **Approval latency.** The owner answers instantly. A real owner takes
  minutes or hours, so a blocked write in model B, or a first write to a
  new room in model A, stalls the agent's work.
- **Anomaly detection.** The brief reserves interrupts for scope expansion
  and for anomalies. There is no anomaly signal here, so the counts cover
  expansion only.
- **Rejections, revocation, expiry.** Every request is approved, and
  nothing expires or gets revoked during the day.
- **Requests for more budget.** A 429 is counted but not acted on. A real
  agent would ask the owner for a larger budget, which is one more
  interrupt per exhausted grant.
- **Multiple agents or plugins, and delegation.** The simulation uses one
  root key on one plugin.
- **Real time.** Simulated hours run in seconds. The key's real-time
  per-minute rate limiter is therefore set out of reach. The per-day
  budget window (24 h) covers the whole 8-hour day either way.
- **Disk.** The sandbox uses an in-memory shared-cache SQLite database,
  with the same schema, statements and transactions as a real one, because
  on-disk SQLite on Windows made a one-hour run take a minute. `--on-disk`
  uses a temp file instead, and a test checks that both give identical
  counts.
