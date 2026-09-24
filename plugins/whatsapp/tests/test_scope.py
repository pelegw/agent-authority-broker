"""Reading the CallScope's visibility: faithful, and closed on anything odd."""

import pytest

from aab_plugin_runtime import AdapterError
from aab_plugin_whatsapp.scope import is_visible, visibility


def test_absent_means_unrestricted():
    assert visibility({}, "chat") == ([], None)
    assert visibility({"visibility": {}}, "chat") == ([], None)
    assert visibility({"visibility": {"contact": {"deny": ["x"]}}}, "chat") == ([], None)


def test_deny_is_deduplicated_in_order():
    v = {"visibility": {"chat": {"deny": ["b", "a", "b"], "allow_only": None}}}
    assert visibility(v, "chat") == (["b", "a"], None)


def test_empty_allow_only_stays_empty():
    # WA_GW turned a key's empty allowlist into None ("unrestricted"). In a
    # CallScope [] means "nothing": coercing it would fail open.
    v = {"visibility": {"chat": {"deny": [], "allow_only": []}}}
    assert visibility(v, "chat") == ([], [])


@pytest.mark.parametrize("bad", [
    None, "scope", {"visibility": []}, {"visibility": {"chat": "a"}},
    {"visibility": {"chat": {"deny": "abc"}}},
    {"visibility": {"chat": {"allow_only": "abc"}}},       # not a list of characters
    {"visibility": {"chat": {"allow_only": [1, 2]}}},
    {"visibility": {"chat": {"deny": [None]}}},
])
def test_malformed_scope_is_400(bad):
    with pytest.raises(AdapterError) as e:
        visibility(bad, "chat")
    assert e.value.status == 400


def test_is_visible_mirrors_the_sql_rule():
    assert is_visible("a", [], None)
    assert not is_visible("a", ["a"], ["a"])               # deny wins
    assert not is_visible("a", [], [])                     # empty allowlist: nothing
    assert is_visible("a", ["b"], ["a"]) and not is_visible("c", [], ["a"])
