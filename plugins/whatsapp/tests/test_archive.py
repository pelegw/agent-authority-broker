"""The archive reader: strictly read-only, empty (not failing) before the
sidecar has written anything, and visibility applied inside the SQL."""

import sqlite3

import pytest

from aab_plugin_whatsapp.archive import Archive, _visibility_clause

from .fakes import ALICE, BOB, CAROL, GROUP, execute


def test_connection_is_read_only(archive):
    with archive.connect_ro() as conn:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO chats (jid) VALUES ('x@s.whatsapp.net')")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM messages")
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1


def test_missing_archive_reads_empty(tmp_path):
    a = Archive(str(tmp_path / "not-yet.db"))
    assert a.exists() is False
    assert a.list_chats() == [] and a.list_contacts() == [] and a.list_messages(ALICE) == []
    assert a.get_chat(ALICE) is None and a.chat_is_visible(ALICE) is False
    assert a.search_messages("x") == [] and a.max_message_rowid() == 0
    assert a.list_events(5, 10, 0) == ([], 5)
    assert a.names([ALICE]) == {}
    assert not (tmp_path / "not-yet.db").exists()        # never created by a reader


@pytest.mark.parametrize("path", ["", "  ", "/data/messages.db?mode=rw", "/data/x.db#frag"])
def test_unsafe_paths_refuse_at_boot(path):
    with pytest.raises(ValueError):
        Archive(path)


def test_repr_names_the_path_only(archive):
    assert repr(archive).startswith("Archive(path=")


# ---- _visibility_clause (unchanged from WA_GW) ------------------------------------

def test_visibility_clause_forms():
    params: list = []
    assert _visibility_clause("jid", [], None, params) == "" and params == []
    params = []
    assert _visibility_clause("jid", ["a", "b"], None, params) == " AND jid NOT IN (?,?)"
    assert params == ["a", "b"]
    params = []
    assert _visibility_clause("jid", [], ["c"], params) == " AND jid IN (?)"
    params = []
    # An explicit empty allowlist means "see nothing", never "unrestricted".
    assert _visibility_clause("jid", [], [], params) == " AND 0" and params == []


def test_deny_wins_over_allow_only(archive):
    assert archive.get_chat(ALICE, deny=[ALICE], allow_only=[ALICE]) is None
    assert [c["jid"] for c in archive.list_chats(deny=[ALICE], allow_only=[ALICE, BOB])] == [BOB]


def test_values_are_parameterized_not_spliced(archive):
    evil = "x') OR 1=1 --"
    assert archive.list_chats(deny=[evil]) != []           # just a non-matching id
    assert archive.list_chats(allow_only=[evil]) == []


# ---- contacts (the addition over WA_GW) ---------------------------------------------

def test_contacts_filter_binds_to_every_match_arm(archive):
    # Unparenthesized, `push_name LIKE ? OR full_name LIKE ? OR jid LIKE ? AND
    # jid NOT IN (...)` would still return Alice through the name arms.
    assert archive.list_contacts("Alice", deny=[ALICE]) == []
    assert [c["jid"] for c in archive.list_contacts("Cohen", deny=[BOB])] == [ALICE]
    assert archive.list_contacts("", allow_only=[]) == []


def test_names_prefer_chat_then_address_book(archive):
    execute(archive.path, "UPDATE chats SET name = '' WHERE jid = ?", (BOB,))
    assert archive.names([ALICE, BOB, CAROL, GROUP, "nobody@s.whatsapp.net"]) == {
        ALICE: "Alice", BOB: "Bob Levi", CAROL: "Carol Stern", GROUP: "Family"}
    assert archive.names([]) == {}


# ---- events (list_events cursor rules) ---------------------------------------------

def test_events_cursor_jumps_past_a_dead_backlog(archive):
    # Nothing fresh: the cursor still moves to the top, so the stale rows are
    # never rescanned on every poll.
    rows, cursor = archive.list_events(0, 10, ts_cutoff=10**12)
    assert rows == [] and cursor == archive.max_message_rowid() == 4


def test_events_cursor_stops_at_the_last_delivered_row(archive):
    rows, cursor = archive.list_events(0, 2, ts_cutoff=0)
    assert [r["id"] for r in rows] == ["A1", "A2"] and cursor == rows[-1]["rowid"] == 2


def test_media_ref_never_selected(archive):
    for row in archive.list_messages(BOB) + archive.search_messages("check"):
        assert "media_ref" not in row and row["has_media"] == 1
    rows, _ = archive.list_events(0, 10, 0)
    assert all("media_ref" not in r for r in rows)
