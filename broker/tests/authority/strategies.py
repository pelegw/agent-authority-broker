"""Hypothesis strategies for capabilities over the echo, WhatsApp and GitHub
manifests. Selector values come from small universes so intersections,
subsets and bottoms all happen often; echo's folder dimension uses the tree
in helpers.PARENT so subtree reasoning is exercised."""

import time

from hypothesis import strategies as st

from broker.authority.capability import Capability, normalize, target_forms

from .helpers import ALL, FOLDERS

UNIVERSE = {
    "room": ["r1", "r2", "r3", "r4"],
    "folder": list(FOLDERS),
    "sender": ["s1", "s2", "*@x.com"],
    "chat": ["c1@s.whatsapp.net", "c2@s.whatsapp.net", "g1@g.us"],
    "repo": ["o/a", "o/b", "o/c"],
    "branch": ["main", "dev", "feat/*"],
}
# Expiries far in the future so live-evaluation tests are not flaky; the pure
# algebra tests still see ordering among them.
FUTURE = int(time.time()) + 10**7


def _scalar(spec):
    if spec.form == "range":
        return st.none() | st.integers(0, 10)
    if spec.form == "flag":
        return st.none() | st.booleans()
    return st.none() | st.sampled_from(spec.values)


@st.composite
def raw_cap(draw, manifest, live=True):
    """A capability as a request might arrive (before normalization)."""
    forms = target_forms(manifest)
    actions = draw(st.sets(st.sampled_from(sorted(manifest.action_names)), min_size=1))
    selector = {}
    constraints = {}
    for name, spec in sorted(forms.items()):
        if spec.form in ("list", "subtree", "pattern"):
            ids = draw(st.none() | st.sets(st.sampled_from(UNIVERSE[name]), min_size=1))
            if ids is not None:
                selector[name] = ids
        else:
            value = draw(_scalar(spec))
            if value is not None:
                constraints[name] = value
    mode = draw(st.sampled_from(["draft", "direct"]))
    expires = draw(st.none() | st.integers(FUTURE, FUTURE + 1000)) if live else draw(
        st.none() | st.integers(0, 1000))
    budget = draw(st.fixed_dictionaries({}, optional={
        "per_minute": st.integers(0, 20), "per_day": st.integers(0, 200)}))
    return Capability(manifest.id, actions, selector, constraints, mode, expires, budget)


@st.composite
def cap_for(draw, manifest, live=True):
    """A normalized capability for one manifest."""
    return draw(st.sampled_from(normalize(draw(raw_cap(manifest, live)), manifest)))


def manifests():
    return st.sampled_from(ALL)


@st.composite
def any_cap(draw, live=True):
    return draw(cap_for(draw(manifests()), live))


@st.composite
def same_target_caps(draw, n, live=False):
    m = draw(manifests())
    return [draw(cap_for(m, live)) for _ in range(n)]


def cap_lists(min_size=1, max_size=5, live=True):
    return st.lists(any_cap(live), min_size=min_size, max_size=max_size)


def denies():
    """Per-key deny sets over the dimensions' resource kinds."""
    return st.fixed_dictionaries({}, optional={
        "echo": st.fixed_dictionaries({}, optional={
            "room": st.lists(st.sampled_from(UNIVERSE["room"]), min_size=1, unique=True),
            "folder": st.lists(st.sampled_from(UNIVERSE["folder"]), min_size=1, unique=True)}),
        "whatsapp": st.fixed_dictionaries({}, optional={
            "chat": st.lists(st.sampled_from(UNIVERSE["chat"]), min_size=1, unique=True)}),
    })
