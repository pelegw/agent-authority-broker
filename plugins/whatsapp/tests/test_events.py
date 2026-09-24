"""check_new_messages: cursor bootstrap, delivery, the history-replay
freshness guard and visibility. Ported from WA_GW tests/test_events.py (the
long-poll `?wait=` loop itself lives in the broker)."""

import time

from .conftest import scope
from .fakes import ALICE, BOB, insert_message


def _poll(perform, cursor=None, call_scope=None, **params):
    if cursor is not None:
        params["cursor"] = cursor
    r = perform("check_new_messages", params, call_scope)
    assert r.status_code == 200, r.text
    return r.json()["data"]


def _bootstrap(perform):
    body = _poll(perform)
    assert body["items"] == []          # never a backlog dump
    return body["cursor"]


def test_bootstrap_returns_top_not_backlog(perform):
    cursor = _bootstrap(perform)
    assert cursor == 4                   # archive seeds exactly 4 messages
    # nothing new yet -> empty, cursor stays put
    assert _poll(perform, cursor) == {"cursor": cursor, "items": []}


def test_new_message_is_delivered_and_cursor_advances(perform, archive):
    cursor = _bootstrap(perform)
    insert_message(archive.path, BOB, "NEW1", "fresh news")
    body = _poll(perform, cursor)
    assert [e["text"] for e in body["items"]] == ["fresh news"]
    assert body["items"][0]["chat_jid"] == BOB
    assert body["items"][0]["resource_ref"] == {"kind": "chat", "id": BOB}
    assert body["cursor"] > cursor
    # delivered once: the new cursor yields nothing
    assert _poll(perform, body["cursor"])["items"] == []


def test_history_sync_replay_is_not_an_event(perform, archive):
    """An old-ts row with a NEW rowid (reconnect history replay) must not be
    delivered — but the cursor must still advance past it (no rescan loop)."""
    cursor = _bootstrap(perform)
    insert_message(archive.path, ALICE, "OLD1", "ancient replay", ts=int(time.time()) - 3600)
    body = _poll(perform, cursor)
    assert body["items"] == []
    assert body["cursor"] > cursor       # jumped past the stale row
    insert_message(archive.path, BOB, "NEW2", "current")
    assert [e["text"] for e in _poll(perform, body["cursor"])["items"]] == ["current"]


def test_hidden_chat_never_appears_in_events(perform, archive):
    cursor = _bootstrap(perform)
    insert_message(archive.path, ALICE, "SECRET1", "private ping")
    insert_message(archive.path, BOB, "PUB1", "public ping")
    body = _poll(perform, cursor, scope(deny=[ALICE]))
    assert [e["text"] for e in body["items"]] == ["public ping"]


def test_allow_only_limits_events(perform, archive):
    cursor = _bootstrap(perform)
    insert_message(archive.path, ALICE, "N1", "to alice")
    insert_message(archive.path, BOB, "N2", "to bob")
    assert [e["text"] for e in _poll(perform, cursor, scope(allow_only=[ALICE]))["items"]] == [
        "to alice"]
    assert _poll(perform, cursor, scope(allow_only=[]))["items"] == []


def test_limit_pages_the_feed_without_skipping(perform, archive):
    cursor = _bootstrap(perform)
    for n in range(5):
        insert_message(archive.path, BOB, f"P{n}", f"page {n}")
    first = _poll(perform, cursor, limit=2)
    second = _poll(perform, first["cursor"], limit=10)
    assert [e["id"] for e in first["items"] + second["items"]] == [f"P{n}" for n in range(5)]


def test_negative_cursor_is_clamped(perform):
    # Treated as 0: the seeded rows are old (not fresh), so nothing is
    # delivered and the cursor jumps to the top instead of erroring.
    assert _poll(perform, -50) == {"cursor": 4, "items": []}
