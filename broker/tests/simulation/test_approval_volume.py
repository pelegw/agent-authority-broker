"""The approval-volume claim (brief §6.3), measured through the real engine:
under standing grants a human is interrupted only for scope expansion, one
interrupt per distinct new room, and that is at most 10% of what
per-action approval costs.

The claim is checked over 3 simulated hours, not 1: in the first hour the
agent is still discovering its world (most of the expansion rooms appear
then), so the ratio is ~8-16% depending on the seed; by hour three it is
under 8% for every seed tried. See docs/approval-volume.md.
"""

import json
import os

import pytest

from broker import db

from . import harness
from .simulate import simulate, table
from .workload import READ, WRITE_KNOWN, WRITE_NEW, generate

SEED = 7


@pytest.fixture(scope="module")
def three_hours():
    return simulate(hours=3, seed=SEED)


def test_standing_interrupts_equal_distinct_new_rooms(three_hours):
    a = three_hours["models"]["standing"]
    distinct = three_hours["workload"]["distinct_new_rooms"]
    assert distinct > 0
    assert a["interrupts"] == a["grant_requests"] == distinct
    assert a["pending_approvals"] == 0
    # Each expansion starts with exactly one out_of_grant deny, the agent's
    # cue to ask; nothing else is ever denied.
    assert a["denies"] == distinct


def test_standing_is_at_most_ten_percent_of_per_action(three_hours):
    a, b = (three_hours["models"][m] for m in ("standing", "per_action"))
    assert b["pending_approvals"] == b["writes"] == three_hours["workload"]["writes"]
    assert b["grant_requests"] == 0
    assert a["interrupts"] <= 0.10 * b["interrupts"]
    assert three_hours["interrupt_ratio"] <= 0.10


def test_standing_interrupts_taper_off(three_hours):
    hourly = three_hours["models"]["standing"]["hourly_interrupts"]
    assert len(hourly) == 3 and sum(hourly) == three_hours["models"]["standing"]["interrupts"]
    assert hourly[0] > hourly[-1]


def test_every_action_ends_performed_or_over_budget():
    # A tiny per_day budget makes both models hit 429s; every action still
    # ends exactly once, either performed or refused for budget.
    s = simulate(hours=1, seed=SEED, per_hour=100, per_day=10)
    for m in s["models"].values():
        assert m["budget_429"] > 0
        assert m["performed"] + m["budget_429"] == m["actions"] == 100


def test_deterministic_under_seed():
    first = simulate(hours=1, seed=3, per_hour=100)
    assert simulate(hours=1, seed=3, per_hour=100) == first
    assert simulate(hours=1, seed=4, per_hour=100) != first
    assert "standing / per-action interrupts" in table(first)


def test_on_disk_matches_in_memory():
    # The in-memory DB is a speed choice only; the counts must not change.
    kw = dict(hours=1, seed=SEED, per_hour=30)
    assert simulate(**kw, on_disk=True) == simulate(**kw)


def test_workload_mix_and_determinism():
    wl = generate(8, SEED)
    n = len(wl.events)
    assert n == 2400 and generate(8, SEED) == wl
    share = {k: sum(e.kind == k for e in wl.events) / n for k in (READ, WRITE_KNOWN, WRITE_NEW)}
    assert abs(share[READ] - 0.70) < 0.03
    assert abs(share[WRITE_KNOWN] - 0.25) < 0.03
    assert abs(share[WRITE_NEW] - 0.05) < 0.015
    assert all(e.room in wl.known_rooms for e in wl.events if e.kind == WRITE_KNOWN)
    assert all(e.room in wl.new_rooms for e in wl.events if e.kind == WRITE_NEW)
    assert not set(wl.known_rooms) & set(wl.new_rooms)


def test_sandbox_restores_process_state():
    before_connect, before_db = db.connect, os.environ.get("BROKER_DB")
    with harness.sandbox():
        assert db.connect is not before_connect
    assert db.connect is before_connect
    assert os.environ.get("BROKER_DB") == before_db


def test_aab_simulate_cli(capsys):
    from cli import aab
    assert aab.main(["simulate", "--hours", "1", "--seed", "3", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["hours"] == 1 and out["seed"] == 3
    assert set(out["models"]) == {"standing", "per_action"}


def test_aab_simulate_outside_the_checkout(tmp_path):
    # Regression: run from another directory, `tests` resolved to the
    # plugin runtime's own tests package (also on sys.path) and the command
    # failed. The CLI must put broker/ first.
    import subprocess
    import sys
    r = subprocess.run([sys.executable, "-W", "ignore", "-m", "cli.aab", "simulate",
                        "--hours", "1", "--seed", "3", "--json"],
                       cwd=tmp_path, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["seed"] == 3
