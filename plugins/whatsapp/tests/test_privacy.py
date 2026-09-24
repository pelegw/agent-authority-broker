"""Chat visibility from the CallScope: a hidden chat must be invisible via
EVERY read path, and a get on it must look exactly like a missing chat.
Ported from WA_GW tests/test_privacy.py (the global private list and a key's
block/allow lists both arrive here as the scope's deny / allow_only)."""

from .conftest import items, scope
from .fakes import ALICE, BOB, GROUP, execute

HIDE_ALICE = scope(deny=[ALICE])


def test_hidden_chat_is_hidden_everywhere(perform):
    jids = [c["jid"] for c in items(perform("list_chats", {}, HIDE_ALICE))]
    assert ALICE not in jids and BOB in jids and GROUP in jids
    assert perform("get_chat", {"chat": ALICE}, HIDE_ALICE).status_code == 404
    # WA_GW answered [] here; hidden == missing now means the missing chat's 404.
    assert perform("read_messages", {"chat": ALICE}, HIDE_ALICE).status_code == 404


def test_hidden_equals_missing_byte_for_byte(perform):
    missing = "999999@s.whatsapp.net"
    for action, extra in [("get_chat", {}), ("read_messages", {}),
                          ("get_media", {"message_id": "A1"})]:
        hid = perform(action, {"chat": ALICE, **extra}, HIDE_ALICE)
        gone = perform(action, {"chat": missing, **extra}, HIDE_ALICE)
        assert hid.status_code == gone.status_code == 404, action
        assert hid.content == gone.content, action
    hid = perform("search_messages", {"query": "lunch", "chat": ALICE}, HIDE_ALICE)
    gone = perform("search_messages", {"query": "lunch", "chat": missing}, HIDE_ALICE)
    assert hid.status_code == gone.status_code == 404 and hid.content == gone.content


def test_search_cannot_leak_hidden_chat(perform):
    # search_messages has no chat filter by default — the bypass most likely
    # to be missed if filtering were bolted onto list paths only.
    assert any("lunch" in m["text"] for m in items(perform("search_messages",
                                                           {"query": "lunch"})))
    assert items(perform("search_messages", {"query": "lunch"}, HIDE_ALICE)) == []


def test_media_of_hidden_chat_never_reaches_sidecar(perform, sidecar):
    assert perform("get_media", {"chat": BOB, "message_id": "B1"}).status_code == 200
    assert sidecar.media_calls == [(BOB, "B1")]
    r = perform("get_media", {"chat": BOB, "message_id": "B1"}, scope(deny=[BOB]))
    assert r.status_code == 404
    r = perform("get_media", {"chat": BOB, "message_id": "B1"}, scope(allow_only=[ALICE]))
    assert r.status_code == 404
    assert sidecar.media_calls == [(BOB, "B1")]            # the sidecar was NOT called again


def test_name_search_respects_filter_or_parens_regression(perform):
    # Under the unparenthesized OR bug, `name LIKE ? OR jid LIKE ? AND jid NOT
    # IN (...)` would return Alice via the name arm despite the deny list.
    assert items(perform("list_chats", {"query": "Alice"}, HIDE_ALICE)) == []
    assert [c["jid"] for c in items(perform("list_chats", {"query": "Bob"}, HIDE_ALICE))] == [BOB]


def test_contacts_stay_visible_by_design(perform):
    # WA_GW's v1 decision, kept: contacts are the name -> JID resolver, so
    # hiding a CHAT does not hide the CONTACT. Locked in so a change is deliberate.
    names = [c["full_name"] for c in items(perform("search_contacts", {"query": "Alice"},
                                                   HIDE_ALICE))]
    assert "Alice Cohen" in names


def test_contact_denies_do_apply_to_contacts(perform):
    r = perform("search_contacts", {"query": "Alice"}, scope(contact_deny=[ALICE]))
    assert items(r) == []


def test_allow_only_restricts_to_exactly_those(perform):
    only_bob = scope(allow_only=[BOB])
    assert [c["jid"] for c in items(perform("list_chats", {}, only_bob))] == [BOB]
    assert perform("get_chat", {"chat": GROUP}, only_bob).status_code == 404
    assert perform("get_chat", {"chat": BOB}, only_bob).status_code == 200
    assert all(m["chat_jid"] == BOB for m in items(perform("search_messages", {"query": "e"},
                                                           only_bob)))


def test_empty_allow_only_sees_nothing(perform):
    # Fail closed: [] is "no chats", never "unrestricted" (see scope.py).
    none = scope(allow_only=[])
    assert items(perform("list_chats", {}, none)) == []
    assert items(perform("search_messages", {"query": "e"}, none)) == []
    assert perform("get_chat", {"chat": BOB}, none).status_code == 404


def test_deny_wins_over_allow_only(perform):
    both = scope(deny=[BOB], allow_only=[BOB, ALICE])
    assert [c["jid"] for c in items(perform("list_chats", {}, both))] == [ALICE]
    assert perform("get_chat", {"chat": BOB}, both).status_code == 404


def test_rename_cannot_unhide(perform, archive):
    """Enforcement is by pinned jid: a group member renaming the chat must not
    resurface a hidden chat."""
    hide_group = scope(deny=[GROUP])
    assert perform("get_chat", {"chat": GROUP}, hide_group).status_code == 404
    execute(archive.path, "UPDATE chats SET name = 'Totally Different' WHERE jid = ?", (GROUP,))
    assert perform("get_chat", {"chat": GROUP}, hide_group).status_code == 404
    assert GROUP not in [c["jid"] for c in items(perform("list_chats", {}, hide_group))]


def test_device_suffix_cannot_reach_a_hidden_chat(perform):
    # The argument is normalized before the visibility check, so ":12" or
    # ".0" suffixes are the same chat, still hidden.
    for alias in ("972501111111:12@s.whatsapp.net", "972501111111.0@s.whatsapp.net",
                  "+972501111111"):
        assert perform("get_chat", {"chat": alias}, HIDE_ALICE).status_code == 404
