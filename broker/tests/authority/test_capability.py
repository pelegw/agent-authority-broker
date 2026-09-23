"""Capability algebra, row by row against the forms table in the plan
(docs/grant-algebra.md), plus normalization and JSON."""

import pytest

from broker.authority.capability import (Capability, cap_le, caps_from_json, caps_to_json,
                                         form_table, from_json, meet, normalize, to_json)

from .helpers import ALL, ECHO, WHATSAPP, tree_ancestors

FORMS = form_table(ALL)
ALL_ECHO = sorted(ECHO.action_names)


def cap(target="echo", actions=("list_items",), mode="direct", **kw):
    return Capability(target, actions, mode=mode, **kw)


def le(a, b, anc=tree_ancestors):
    return cap_le(a, b, FORMS, anc)


def mt(a, b, anc=tree_ancestors):
    return meet(a, b, FORMS, anc)


# ---- base axes -------------------------------------------------------------------

def test_target_must_match():
    wa = Capability("whatsapp", ["list_chats"])
    assert not le(cap(), wa) and mt(cap(), wa) is None


def test_unknown_target_fails_closed():
    x = Capability("ghost", ["a"])
    assert not le(x, x) and mt(x, x) is None


def test_actions_subset_and_intersection():
    small, big = cap(actions=["list_items"]), cap(actions=["list_items", "get_item"])
    assert le(small, big) and not le(big, small)
    assert mt(small, big) == small
    assert mt(cap(actions=["get_item"]), cap(actions=["watch"])) is None   # ∅ -> ⊥


def test_mode_draft_below_direct():
    d, D = cap(actions=["post_item"], mode="draft"), cap(actions=["post_item"])
    assert le(d, D) and not le(D, d)
    assert mt(d, D).mode == "draft"


def test_expiry_none_is_infinite():
    never, soon = cap(), cap(expires_at=100)
    assert le(soon, never) and not le(never, soon)
    assert le(cap(expires_at=50), soon) and not le(cap(expires_at=150), soon)
    assert mt(never, soon).expires_at == 100
    assert mt(cap(expires_at=50), soon).expires_at == 50


def test_budget_missing_is_unlimited():
    unl, lim = cap(), cap(budget={"per_day": 10})
    assert le(lim, unl) and not le(unl, lim)
    assert not le(cap(budget={"per_day": 11}), lim)
    assert le(cap(budget={"per_day": 3, "per_minute": 1}), lim)
    m = mt(cap(budget={"per_day": 5}), cap(budget={"per_day": 9, "per_minute": 2}))
    assert dict(m.budget) == {"per_day": 5, "per_minute": 2}


# ---- list ----------------------------------------------------------------------------

def test_list_star_parent_accepts_all():
    assert le(cap(selector={"room": ["r1"]}), cap())
    assert le(cap(selector={"room": ["r1"]}), cap(selector={"room": "*"}))


def test_list_subset():
    p = cap(selector={"room": ["r1", "r2"]})
    assert le(cap(selector={"room": ["r1"]}), p)
    assert not le(cap(selector={"room": ["r1", "r3"]}), p)
    assert not le(cap(), p)                     # child "*" exceeds explicit parent


def test_list_meet_intersection_and_empty_is_bottom():
    a, b = cap(selector={"room": ["r1", "r2"]}), cap(selector={"room": ["r2", "r3"]})
    assert mt(a, b).selector["room"] == {"r2"}
    assert mt(a, cap(selector={"room": ["r9"]})) is None
    assert mt(a, cap()).selector["room"] == {"r1", "r2"}      # "*" ∩ X = X


# ---- subtree -----------------------------------------------------------------------

def test_subtree_child_roots_inside_parent_roots():
    p = cap(selector={"folder": ["a"]})
    assert le(cap(selector={"folder": ["a1x", "a2"]}), p)
    assert le(cap(selector={"folder": ["a"]}), p)
    assert not le(cap(selector={"folder": ["b1"]}), p)
    assert not le(cap(selector={"folder": ["root"]}), p)      # the parent's parent is wider


def test_subtree_default_ancestry_is_exact_only():
    p = cap(selector={"folder": ["a"]})
    no_tree = lambda kind, rid: ()   # noqa: E731
    assert not le(cap(selector={"folder": ["a1"]}), p, anc=no_tree)
    assert le(cap(selector={"folder": ["a"]}), p, anc=no_tree)


def test_subtree_meet_keeps_inner_roots():
    a = cap(selector={"folder": ["a", "b1"]})
    b = cap(selector={"folder": ["a1", "b"]})
    assert mt(a, b).selector["folder"] == {"a1", "b1"}
    assert mt(cap(selector={"folder": ["a"]}), cap(selector={"folder": ["b"]})) is None
    # redundant roots are pruned, so the representation is canonical
    assert mt(cap(selector={"folder": ["a", "a1"]}), cap()).selector["folder"] == {"a"}


# ---- pattern ----------------------------------------------------------------------

def test_pattern_is_exact_string_subset():
    p = cap(selector={"sender": ["*@x.com", "bob@y.com"]})
    assert le(cap(selector={"sender": ["bob@y.com"]}), p)
    # no glob implication: "a@x.com" matches "*@x.com" as a glob but is not a
    # member of the pattern set, so it is NOT <= (conservative and decidable).
    assert not le(cap(selector={"sender": ["a@x.com"]}), p)
    assert mt(p, cap(selector={"sender": ["bob@y.com", "z"]})).selector["sender"] == {"bob@y.com"}
    assert mt(p, cap(selector={"sender": ["q"]})) is None


# ---- range ---------------------------------------------------------------------------

def test_range_child_le_parent_and_min():
    p = cap(constraints={"window_days": 30})
    assert le(cap(constraints={"window_days": 7}), p)
    assert not le(cap(constraints={"window_days": 31}), p)
    assert not le(cap(), p)                     # absent == unbounded
    assert le(p, cap())
    assert mt(p, cap(constraints={"window_days": 3})).constraints["window_days"] == 3
    assert mt(p, cap()).constraints["window_days"] == 30


# ---- flag -----------------------------------------------------------------------------

def test_flag_parent_false_forces_child_false():
    no = cap(actions=["post_item"], constraints={"attachments": False})
    yes = cap(actions=["post_item"], constraints={"attachments": True})
    free = cap(actions=["post_item"])
    assert le(no, yes) and le(no, free) and le(yes, free)
    assert not le(yes, no) and not le(free, no)
    assert mt(no, yes).constraints["attachments"] is False
    # true is top, so it is dropped from the result
    assert "attachments" not in mt(yes, free).constraints


# ---- level ------------------------------------------------------------------------------

def test_level_rank_and_min():
    lo, hi = cap(constraints={"visibility": "summary"}), cap(constraints={"visibility": "full"})
    assert le(lo, hi) and not le(hi, lo) and le(lo, cap())
    assert mt(lo, hi).constraints["visibility"] == "summary"
    assert "visibility" not in mt(hi, cap()).constraints      # top dropped


# ---- fail closed on things the lattice cannot read ---------------------------------------

def test_unknown_dimension_or_bad_value_fails_closed():
    ghost = cap(selector={"ghost": ["x"]})
    assert not le(ghost, ghost) and mt(ghost, cap()) is None
    bad_level = cap(constraints={"visibility": "ultra"})
    assert not le(bad_level, cap()) and mt(bad_level, cap()) is None
    bad_range = cap(constraints={"window_days": True})
    assert not le(bad_range, cap()) and mt(bad_range, cap()) is None
    # a constraint name used as a selector dimension (wrong slot)
    wrong_slot = cap(selector={"window_days": ["3"]})
    assert mt(wrong_slot, cap()) is None and not le(wrong_slot, cap())


def test_bottom_is_not_representable():
    with pytest.raises(ValueError):
        Capability("echo", [])
    with pytest.raises(ValueError):
        Capability("echo", ["list_items"], selector={"room": []})
    with pytest.raises(ValueError):
        Capability("echo", "list_items")       # a bare string is not a list


def test_capability_is_immutable_and_hashable():
    c = cap(selector={"room": ["r1"]})
    with pytest.raises(AttributeError):
        c.mode = "draft"
    with pytest.raises(TypeError):
        c.selector["room"] = frozenset({"r2"})
    assert c == cap(selector={"room": ["r1"]}) and len({c, cap(selector={"room": ["r1"]})}) == 1


# ---- normalization -------------------------------------------------------------------------

def test_normalize_expands_globs_and_drops_star():
    [n] = normalize(Capability("echo", ["read_*"], selector={"room": "*"}), ECHO)
    assert n.actions == ECHO.actions_by_effect("read") and "room" not in n.selector


def test_normalize_forces_reads_direct_and_splits_mixed_draft():
    [n] = normalize(Capability("echo", ["list_items"], mode="draft"), ECHO)
    assert n.mode == "direct"
    parts = normalize(Capability("echo", ["*"], mode="draft"), ECHO)
    by_mode = {p.mode: p.actions for p in parts}
    assert by_mode == {"direct": ECHO.actions_by_effect("read"),
                       "draft": ECHO.actions_by_effect("write", "destructive")}


def test_normalize_drops_top_constraints_and_validates():
    [n] = normalize(Capability("echo", ["*"], constraints={
        "attachments": True, "visibility": "full", "window_days": 5}), ECHO)
    assert dict(n.constraints) == {"window_days": 5}
    for bad in ({"visibility": "ultra"}, {"window_days": "3"}, {"window_days": -1},
                {"ghost": 1}, {"attachments": 1}):
        with pytest.raises(ValueError):
            normalize(Capability("echo", ["*"], constraints=bad), ECHO)
    with pytest.raises(ValueError):
        normalize(Capability("echo", ["*"], selector={"window_days": ["1"]}), ECHO)
    with pytest.raises(ValueError):
        normalize(Capability("echo", ["nope"]), ECHO)
    with pytest.raises(ValueError):
        normalize(Capability("whatsapp", ["*"]), ECHO)


def test_normalize_rejects_derived_dimension():
    from .helpers import GITHUB
    with pytest.raises(ValueError):
        normalize(Capability("github", ["*"], selector={"permissions": ["read"]}), GITHUB)


def test_normalize_is_idempotent():
    for c in (Capability("echo", ["*"], mode="draft", selector={"room": ["r1"]}),
              Capability("whatsapp", ["read_*", "send_message"], selector={"chat": ["c"]})):
        m = ECHO if c.target == "echo" else WHATSAPP
        once = normalize(c, m)
        assert [x for n in once for x in normalize(n, m)] == once


# ---- JSON ------------------------------------------------------------------------------------

def test_json_round_trip_and_canonical_shape():
    c = Capability("echo", ["post_item", "list_items"], selector={"room": ["r2", "r1"]},
                   constraints={"window_days": 3}, mode="direct", expires_at=99,
                   budget={"per_day": 4})
    d = to_json(c)
    assert d["actions"] == ["list_items", "post_item"] and d["selector"] == {"room": ["r1", "r2"]}
    assert from_json(d) == c
    assert caps_from_json(caps_to_json([c, c])) == [c, c]


def test_from_json_rejects_unknown_fields_and_bad_shapes():
    base = to_json(cap())
    for bad in ({**base, "deny": {}}, {**base, "mode": "god"}, {**base, "actions": "x"},
                {**base, "budget": {"per_hour": 1}}, {**base, "expires_at": "soon"},
                {"actions": ["x"]}, [], {**base, "selector": ["room"]}):
        with pytest.raises(ValueError):
            from_json(bad)
