# Approval volume: standing grants vs per-action approval

Human-in-the-loop control fails when there is too much of it. The approver
starts to rubber-stamp. Then the approvals are a record of oversight that
did not really occur (brief §6.3). The broker's answer is **standing
narrow grants**. The agent asks one time for a bounded capability. The
broker interrupts a human only when the agent needs more scope. This
simulation measures how many interrupts that design causes.

## Running it

```bash
cd broker
python -m tests.simulation.simulate --hours 8            # table + JSON summary
python -m tests.simulation.simulate --hours 8 --json     # JSON only
aab simulate --hours 8 --seed 7                          # same thing, from a source checkout
```

The module form also accepts `--per-hour`, `--per-day` (the write budget,
default 1000), `--new-pool` and `--on-disk`. For a given seed, the output is
always the same. The test `tests/simulation/test_approval_volume.py` runs a
3-hour workload in the normal suite.

## What it models

**Workload** (`tests/simulation/workload.py`). The workload is a seeded
synthetic day of 300 actions an hour on the in-process `echo` plugin. This
table gives the mix:

| Share | Action | Resource |
|---|---|---|
| 70% | `list_items` (read) | any room: the 20 known rooms and the 20 expansion rooms |
| 25% | `post_item` (write) | one of the 20 **known** rooms, `r1`–`r20` |
| 5% | `post_item` (write) | one of 20 **expansion** rooms, `r21`–`r40` |

The expansion rooms are a finite pool, so the agent's world has a fixed
size. The first write to a given expansion room is scope expansion. Later
writes to that room are ordinary work. The simulation makes the workload one
time and replays it unchanged through both models.

**Model A: standing grant.** The key starts with one root grant. It permits
reads direct on `*`. It permits writes direct on the 20 known rooms, with
`budget.per_day` = 1000 (the default; `--per-day` changes it). A write to an
expansion room gets a 403 `out_of_grant` (hint `request_permission`). The
agent then asks for `post_item` on exactly that room. The request has the
same budget and an explicit `"mode": "direct"`. An agent request without a
mode asks for draft. The owner approves through `decide_grant`, and the
agent retries. From then on, writes to that room are direct.

**Model B: per-action.** The reads are the same. Writes use draft mode on
every room. Thus each write goes into the queue as a pending action. The
owner approves it through `approve_action`.

Everything goes through `engine.perform` and the real admin services:

- Policy evaluation.
- The hash-chained decision record.
- Drafts and the action queue.
- The capacity ledger.
- Grant requests.

A simulated owner approves every request immediately.

**Counted:** The simulation counts human interrupts (pending actions plus
grant requests), interrupts per hour, denies to the agent, and budget 429s.

## Results (8 hours, seed 7)

This run used Windows 11 and Python 3.14.6 (AMD64), with the default
`per_day` budget of 1000. The run took about 18 s.

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
  (20). The only denies are the 20 `out_of_grant` responses that started
  each request.
- **The standing-grant cost is front-loaded.** 14 of the 20 interrupts come
  in the first hour, while the agent discovers its world. After the third
  hour there is only one more. Per-action approval costs about 90
  interrupts every hour, with no end. That is the rubber-stamping regime.
- **Over one hour, the ratio does not meet the 10% target.** Across seeds
  1–8, the one-hour ratio goes from 7.9% to 15.6%. Over 2 hours it is
  7.0–9.9%, and over 3 hours it is 6.0–7.5%. The advantage builds up over
  time. It is not there from the first minute. For that reason the suite
  checks the claim over 3 hours.

### What budget exhaustion looks like

This is the same run with `--per-day 200`. The implementation plan used 200
as an example. That value is too small for this workload.

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

- **The interrupt counts do not change.** The simulation counts an
  interrupt when it asks a human. It does not matter if the broker delivers
  the action after that.
- **Model A runs out of budget at about 2 h 40 min.** The workload makes
  about 75 known-room writes an hour against the root grant's 200 a day.
  After that, 392 known-room writes fail with 429. Writes to expansion
  rooms continue to work. Each expansion grant has a budget of its own (see
  the design observation below).
- **In model B, the owner wastes approvals.** The owner approves 512
  actions that then fail with 429 at delivery. Each of those actions goes
  back to `pending`. Those approvals interrupted the owner and did nothing.
  Neither model counts a request for more budget as an interrupt (see
  below).

### Design observation: capacity grows with approvals

Budgets belong to grants, not to keys. Each approved expansion grant brings
its own `per_day` budget. The ledger charges a write only to the grant chain
that authorized it. After the 20 expansions of model A, the total daily
write capacity of the key is 21 × `per_day`. That is the root grant plus one
budget per expansion room. A human approved each of those grants, so the key
got no authority without consent. But an approver reads a request as "can
post in room r27". The approver does not read it as "can make another 1000
writes a day", and nothing on the request says so. There are two possible
responses:

- Show the budget of a request on its approval card.
- Add a key-level ceiling that caps the daily writes of the key, whatever
  the number of grants it holds.

## What it does not model

- **Approval latency.** The owner answers instantly. A real owner takes
  minutes or hours. In that time, a blocked write in model B stops the
  agent's work. A first write to a new room in model A also stops it.
- **Anomaly detection.** The brief keeps interrupts for scope expansion
  and for anomalies. There is no anomaly signal here, so the counts cover
  expansion only.
- **Rejections, revocation, expiry.** The owner approves every request.
  No grant expires, and the owner revokes nothing during the day.
- **Requests for more budget.** The simulation counts a 429 but does
  nothing about it. A real agent asks the owner for a larger budget. That
  is one more interrupt per exhausted grant.
- **Multiple agents or plugins, and delegation.** The simulation uses one
  root key on one plugin.
- **Real time.** Simulated hours run in seconds. Thus the simulation sets
  the real-time per-minute rate limiter of the key out of reach. The
  per-day budget window (24 h) covers the whole 8-hour day either way.
- **Disk.** The sandbox uses an in-memory shared-cache SQLite database. It
  has the same schema, statements and transactions as a real one. On
  Windows, on-disk SQLite made a one-hour run take a minute. `--on-disk`
  uses a temp file instead. A test checks that both give identical counts.
