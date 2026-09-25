"""role_ceiling: the role as a ceiling, spelled out per action.

The module only describes; policy.evaluate decides. So the core test here
runs the real engine for every ceiling, capability mode and side effect
(including a write that cannot be drafted) and requires the description to
match the decision exactly. The rest pins the helpers the cards, the admin
grant list and get_my_access build on.
"""

import time
from pathlib import Path

import pytest

from broker import role_ceiling
from broker.authority.capability import Capability
from broker.authority.roles import ROLES
from broker.plugins.manifest import load_manifest
from broker.policy import evaluate

from .conftest import ECHO_DIR, cap

ECHO = load_manifest(ECHO_DIR / "manifest.yaml")
GITHUB = load_manifest(Path(__file__).resolve().parents[1] / "broker" / "targets" / "github"
                       / "manifest.yaml")
# One action per side effect, plus touch_item: a write that cannot be drafted.
CALLS = {"list_items": {}, "post_item": {"room": "r1", "text": "hi"},
         "delete_item": {"item_id": "i1"}, "touch_item": {"item_id": "i1"}}
ALL = sorted(CALLS)


def _decided(agent, action) -> str | None:
    d = evaluate(agent.auth, "echo", action, CALLS[action], int(time.time()))
    if d.decision == "deny":
        assert d.status == 403, (action, d.reason)
        return None
    return "direct" if d.decision == "allow" else "draft"


@pytest.mark.parametrize("mode", ["direct", "draft"])
@pytest.mark.parametrize("role", ROLES)
def test_mode_under_matches_the_engine(echo_local, make_agent, role, mode):
    a = make_agent([cap(ALL, mode=mode)], role=role)
    for action in ALL:
        assert role_ceiling.mode_under(ECHO, action, mode, role) == _decided(a, action), \
            (role, mode, action)


def test_lowered_names_only_what_the_ceiling_changes():
    direct = Capability("echo", ALL, mode="direct")
    assert role_ceiling.lowered(ECHO, direct, "full") == {}
    assert role_ceiling.lowered(ECHO, direct, "read-act") == {"delete_item": "draft"}
    # touch_item cannot be drafted, so a draft ceiling denies it outright.
    assert role_ceiling.lowered(ECHO, direct, "read-draft") == {
        "delete_item": "draft", "post_item": "draft", "touch_item": "denied"}
    assert role_ceiling.lowered(ECHO, direct, "read-only") == {
        "delete_item": "denied", "post_item": "denied", "touch_item": "denied"}
    # A draft capability over touch_item cannot run by itself: not the
    # ceiling's doing, so never reported as lowered.
    draft = Capability("echo", ["post_item", "touch_item"], mode="draft")
    assert role_ceiling.lowered(ECHO, draft, "read-draft") == {}
    assert role_ceiling.lowered(ECHO, draft, "read-only") == {"post_item": "denied"}


def test_unreadable_input_is_never_shown_as_runnable():
    assert role_ceiling.mode_under(ECHO, "no_such_action", "direct", "full") is None
    assert role_ceiling.mode_under(ECHO, "list_items", "direct", "god") is None
    assert role_ceiling.mode_under(ECHO, "list_items", "sideways", "full") is None


def test_key_ceiling_is_the_lowest_role_in_the_chain():
    assert role_ceiling.key_ceiling(["full"]) == "full"
    assert role_ceiling.key_ceiling(["read-draft", "full", "read-act"]) == "read-draft"
    assert role_ceiling.key_ceiling(["full", "read-only"]) == "read-only"
    assert role_ceiling.key_ceiling([]) is None
    # An unknown role ranks lowest (and mode_under caps everything under it).
    assert role_ceiling.key_ceiling(["full", "god"]) == "god"


@pytest.mark.parametrize("low", ROLES)
@pytest.mark.parametrize("high", ROLES)
def test_meeting_two_ceilings_is_meeting_the_lower(low, high):
    """Why key_ceiling may reduce a chain to one role: for every action and
    capability mode, the mode under both ceilings (the lower of the two
    bounds) equals the mode under the lower ceiling alone."""
    lower = role_ceiling.key_ceiling([low, high])
    for action in ALL:
        for mode in ("direct", "draft"):
            under_low = role_ceiling.mode_under(ECHO, action, mode, low)
            both = (role_ceiling.mode_under(ECHO, action, under_low, high)
                    if under_low is not None else None)
            assert both == role_ceiling.mode_under(ECHO, action, mode, lower)


def test_grant_lowered_labels_by_target_when_a_grant_spans_several():
    manifests = {"echo": ECHO, "github": GITHUB}
    post = Capability("echo", ["post_item"], mode="direct")
    assert role_ceiling.grant_lowered(manifests, [post], "read-draft") == {"post_item": "draft"}
    write = next(a.name for a in GITHUB.actions if a.side_effect == "write")
    both = role_ceiling.grant_lowered(
        manifests, [post, Capability("github", [write], mode="direct")], "read-only")
    assert both == {"echo.post_item": "denied", f"github.{write}": "denied"}
    # A capability on a plugin that is not registered is skipped, not guessed.
    assert role_ceiling.grant_lowered({"echo": ECHO}, [Capability("gone", ["x"])],
                                      "read-only") == {}


def test_note_says_what_approving_will_not_do():
    assert role_ceiling.note("full", {}) is None
    text = role_ceiling.note("read-draft", {"post_item": "draft", "touch_item": "denied"})
    assert text.startswith("This key's ceiling is read-draft: ")
    assert "post_item will still queue for your approval" in text
    assert "touch_item will stay denied" in text
    assert "Raise the key's ceiling" in text


def test_uncapped_view_lifts_only_the_roles(echo_local, make_agent):
    """The display-only view is the live evaluation with every role lifted to
    full: same capabilities as a full key, same chains, and it leaves the
    caller's context untouched."""
    from broker.authority.effective import effective_with_chains
    from broker.plugins.registry import get_registry
    reg = get_registry()
    now = int(time.time())
    capped = make_agent([cap(ALL, mode="direct")], role="read-draft")
    full = make_agent([cap(ALL, mode="direct")], role="full")
    lifted = role_ceiling.uncapped_with_chains(capped.auth, now, reg.plugin_states(),
                                               reg.lattice())
    reference = effective_with_chains(full.auth, now, reg.plugin_states(), reg.lattice())
    assert [c for c, _ in lifted] == [c for c, _ in reference]
    assert [chain for _, chain in lifted] == [(capped.grant_id,)]
    assert capped.auth.role == "read-draft" and capped.auth.chain_roles == ("read-draft",)
    # The real evaluation of the capped key is unchanged: writes still draft.
    assert _decided(capped, "post_item") == "draft"
