"""policy.evaluate: every branch, against the echo plugin."""

import time

import pytest
import yaml

from broker import hidden
from broker.plugins import settings
from broker.plugins.manifest import Manifest
from broker.policy import enforced_where, evaluate

from .conftest import ECHO_DIR, cap, enable_plugin


def ev(agent, action, params, **kw):
    return evaluate(agent.auth, kw.pop("target", "echo"), action, params, int(time.time()), **kw)


def test_allow_carries_chain_scope_and_enforced_where(echo_local, make_agent):
    a = make_agent([cap(["post_item"], selector={"room": ["r1"]})])
    d = ev(a, "post_item", {"room": "r1", "text": "hi"})
    assert (d.decision, d.status, d.reason) == ("allow", 200, "covered")
    assert d.grant_chain_ids == (a.grant_id,)
    assert d.resource == "r1"
    assert d.scope.visibility["room"] == {"deny": [], "allow_only": ["r1"]}
    assert d.scope.credential == {"permissions": {"items": "write"}}
    assert d.enforced_where["room"] == "proxy" and d.enforced_where["mode"] == "proxy"


def test_disabled_target_is_404_like_unknown(echo_local, make_agent):
    a = make_agent([cap(["list_items"])])
    settings.set_enabled("echo", False)
    disabled = ev(a, "list_items", {})
    unknown = ev(a, "list_items", {}, target="nope")
    assert (disabled.decision, disabled.status) == ("deny", 404)
    assert (disabled.message, disabled.code) == (unknown.message, unknown.code)


def test_unknown_action_is_404(echo_local, make_agent):
    d = ev(make_agent([cap(["list_items"])]), "no_such", {})
    assert (d.decision, d.status, d.reason) == ("deny", 404, "unknown_action")


def test_invalid_params_are_400_without_echoing_values(echo_local, make_agent):
    a = make_agent([cap(["post_item"])])
    d = ev(a, "post_item", {"room": "r1", "text": "x", "smuggled": "secret-value"})
    assert (d.decision, d.status, d.code) == ("deny", 400, "invalid_params")
    assert "secret-value" not in d.message
    assert ev(a, "post_item", "not-an-object").status == 400


def test_not_connected_is_503(echo_local, make_agent):
    a = make_agent([cap(["list_items"])])
    enable_plugin(connected=False)
    d = ev(a, "list_items", {})
    assert (d.decision, d.status, d.code) == ("deny", 503, "not_connected")


def test_selector_is_normalized_by_the_plugin(echo_local, make_agent):
    a = make_agent([cap(["post_item"], selector={"room": ["r1"]})])
    d = ev(a, "post_item", {"room": " R1 ", "text": "x"})
    assert d.decision == "allow" and d.params["room"] == "r1"
    bad = ev(a, "post_item", {"room": "lobby", "text": "x"})
    assert (bad.status, bad.code) == (400, "invalid_params")


def test_hidden_resource_is_404_identical_to_missing(echo_local, make_agent):
    a = make_agent([cap(["post_item"])])
    hidden.add("echo", "room", "r2")
    d = ev(a, "post_item", {"room": "r2", "text": "x"})
    assert (d.decision, d.status, d.message, d.code, d.reason) == (
        "deny", 404, "not found", "not_found", "hidden")


def test_key_deny_is_404(echo_local, make_agent):
    a = make_agent([cap(["post_item"])], denies={"echo": {"room": ["r3"]}})
    assert ev(a, "post_item", {"room": "r3", "text": "x"}).status == 404
    assert ev(a, "post_item", {"room": "r1", "text": "x"}).decision == "allow"


def test_out_of_grant_is_403(echo_local, make_agent):
    a = make_agent([cap(["post_item"], selector={"room": ["r1"]})])
    d = ev(a, "post_item", {"room": "r2", "text": "x"})
    assert (d.decision, d.status, d.code, d.hint) == (
        "deny", 403, "out_of_grant", "request_permission")
    # An action the grant does not name at all is out of grant too.
    assert ev(a, "delete_item", {"item_id": "i1"}).status == 403


def test_no_grants_at_all_is_403(echo_local, make_agent):
    assert ev(make_agent(), "list_items", {}).status == 403


def test_draft_mode_capability_drafts(echo_local, make_agent):
    a = make_agent([cap(["post_item"], mode="draft")])
    d = ev(a, "post_item", {"room": "r1", "text": "x"})
    assert (d.decision, d.status, d.reason) == ("draft", 202, "draft_mode")


def test_as_draft_drafts_a_direct_capability(echo_local, make_agent):
    a = make_agent([cap(["post_item"])])
    d = ev(a, "post_item", {"room": "r1", "text": "x"}, as_draft=True)
    assert (d.decision, d.reason) == ("draft", "as_draft")


def test_as_draft_on_an_undraftable_action_is_400(echo_local, make_agent):
    a = make_agent([cap(["list_items", "touch_item"])])
    assert ev(a, "list_items", {}, as_draft=True).code == "draft_unsupported"
    assert ev(a, "touch_item", {"item_id": "i1"}, as_draft=True).status == 400


def test_draft_only_authority_cannot_reach_a_direct_only_action(echo_local, make_agent):
    # touch_item has modes [direct]; a draft-mode capability cannot run it.
    a = make_agent([cap(["touch_item"], mode="draft")])
    d = ev(a, "touch_item", {"item_id": "i1"})
    assert (d.decision, d.status, d.reason) == ("deny", 403, "mode_unsupported")
    # Same through the role: read-draft caps writes at draft.
    b = make_agent([cap(["touch_item"])], role="read-draft")
    assert ev(b, "touch_item", {"item_id": "i1"}).reason == "mode_unsupported"


def test_direct_capability_preferred_over_draft(echo_local, make_agent):
    a = make_agent([cap(["post_item"], mode="draft"),
                    cap(["post_item"], selector={"room": ["r1"]})])
    assert ev(a, "post_item", {"room": "r1", "text": "x"}).decision == "allow"
    assert ev(a, "post_item", {"room": "r2", "text": "x"}).decision == "draft"


def test_schedule_only_where_schedulable(echo_local, make_agent):
    a = make_agent([cap(["post_item", "delete_item"])])
    assert ev(a, "post_item", {"room": "r1", "text": "x"}, scheduled=True).decision == "allow"
    assert ev(a, "delete_item", {"item_id": "i1"}, scheduled=True).code == "not_schedulable"


def test_selector_dimension_applies_only_to_its_actions(echo_local, make_agent):
    # `sender` applies to list_items and post_item, not get_item.
    a = make_agent([cap(["list_items", "get_item"], selector={"sender": ["s1"]})])
    listed = ev(a, "list_items", {})
    got = ev(a, "get_item", {"item_id": "i2"})
    assert listed.scope.visibility["sender"]["allow_only"] == ["s1"]
    assert got.decision == "allow" and "sender" not in got.scope.visibility
    # `room` does not apply to get_blob: a room selector does not block it.
    b = make_agent([cap(["get_blob"], selector={"room": ["r1"]})])
    blob = ev(b, "get_blob", {"item_id": "i2"})
    assert blob.decision == "allow" and "room" not in blob.scope.visibility


def test_constraints_passed_only_where_they_apply(echo_local, make_agent):
    a = make_agent([cap(["list_items", "post_item"],
                        constraints={"window_days": 7, "attachments": False})])
    listed = ev(a, "list_items", {})
    posted = ev(a, "post_item", {"room": "r1", "text": "x"})
    assert listed.scope.constraints == {"window_days": 7}
    assert posted.scope.constraints == {"attachments": False}


def test_list_read_gets_selector_as_allow_only(echo_local, make_agent):
    a = make_agent([cap(["list_items"], selector={"room": ["r1", "r2"], "folder": ["a"]})])
    d = ev(a, "list_items", {})
    assert d.scope.visibility["room"]["allow_only"] == ["r1", "r2"]
    assert d.scope.visibility["folder"]["allow_only"] == ["a"]


def test_hidden_folder_hides_its_subtree(echo_local):
    anc = lambda kind, rid: {"a1x": ["a1", "a", "root"]}.get(rid, [])  # noqa: E731
    assert hidden.is_denied({"a"}, "a1x", "folder", anc)
    assert not hidden.is_denied({"b"}, "a1x", "folder", anc)


def _variant(**changes) -> Manifest:
    data = yaml.safe_load((ECHO_DIR / "manifest.yaml").read_text(encoding="utf-8"))
    data["connection"]["enforcement"] = changes.get("connection", "proxy")
    for n in data["narrowings"]:
        if n["dimension"] == "room":
            n["enforcement"] = changes.get("room", "proxy")
    return Manifest.model_validate(data)


def test_enforced_where_from_manifest_and_live_mode():
    m = _variant(connection="target", room="target")
    assert enforced_where(m, "post_item", {})["room"] == "target"
    assert enforced_where(m, "post_item", {})["sender"] == "proxy"
    # The connection reports proxy mode right now (e.g. a PAT fallback).
    assert enforced_where(m, "post_item", {"enforcement": "proxy"})["room"] == "proxy"
    # A proxy connection can never claim target enforcement.
    assert enforced_where(_variant(room="target"), "post_item", {})["room"] == "proxy"
    # Only dimensions that bound the action are reported.
    assert "folder" not in enforced_where(m, "watch", {})


@pytest.mark.parametrize("health,expected", [
    ({}, "target"),                                        # never reported: manifest stands
    ({"enforcement": "target", "healthy": True}, "target"),
    ({"enforcement": "mixed"}, "target"),                  # plugin-google: per the manifest
    ({"enforcement": "proxy"}, "proxy"),
    # A record that does not say fails closed: a refresh error, a plugin
    # that omits the field, an unknown value.
    ({"healthy": False, "error": "plugin service unreachable", "status": 503}, "proxy"),
    ({"connected": True, "healthy": True}, "proxy"),
    ({"enforcement": "banana"}, "proxy"),
    ({"healthy": False, "error": "down", "status": 503, "enforcement": "target"}, "target"),
])
def test_enforced_where_claims_target_only_when_the_plugin_says_so(health, expected):
    m = _variant(connection="target", room="target")
    assert enforced_where(m, "post_item", health)["room"] == expected


def test_without_authority_hidden_and_nonexistent_look_the_same(echo_local, make_agent):
    # A key whose grant does not reach the resource gets 403 either way, so
    # the status never reveals that a hidden resource exists.
    a = make_agent([cap(["post_item"], selector={"room": ["r1"]})])
    hidden.add("echo", "room", "r2")
    hid = ev(a, "post_item", {"room": "r2", "text": "x"})
    nonexistent = ev(a, "post_item", {"room": "r9", "text": "x"})
    assert (hid.status, hid.message, hid.code) == (
        nonexistent.status, nonexistent.message, nonexistent.code) == (
        403, "not covered by any of your grants", "out_of_grant")
