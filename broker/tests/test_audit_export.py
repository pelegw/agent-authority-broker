"""The audit exporter (broker/audit_export.py, `aab audit export`): every
`decisions` and `audit_log` row exactly once across runs, in the documented
shape, read through a read-only connection; the cursor moves only after a
batch is out, survives a crash without a gap, and notices a replaced
database; the hashing option hides every resource id; a missing, empty or
malformed database exports nothing and moves nothing."""

import hashlib
import io
import json
import logging
import re
import sqlite3
import threading
from pathlib import Path

import pytest

from broker import audit, audit_export, db, decisions, engine
from broker.config import get_settings

from .conftest import cap

RESOURCE = "15551230000@s.whatsapp.net"


def _db() -> Path:
    return Path(get_settings().broker_db)


def _decision(n: int = 1, resource: str = RESOURCE, reason: str = "out_of_grant") -> None:
    for i in range(n):
        decisions.record(request_id=f"req-{i}", kind="decision", target="whatsapp",
                         action="send_message", resource=resource,
                         params={"text": "private words"}, decision="deny", reason=reason,
                         key_id=7, key_name="bot")


def _audit_row(detail=None, resource: str = f"whatsapp:chat:{RESOURCE}") -> None:
    audit.audit("owner", "hidden.add", resource,
                detail if detail is not None else {"reason": "family", "count": 3})


def _columns(table: str) -> list[str]:
    with db.connect() as conn:
        return [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]


def _rows(table: str) -> list[dict]:
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY id")]


def _export(state: Path, **kw) -> list[dict]:
    out = io.StringIO()
    audit_export.export(_db(), state, out, **kw)
    return [json.loads(line) for line in out.getvalue().splitlines()]


@pytest.fixture()
def state(tmp_path) -> Path:
    d = tmp_path / "audit-export"
    d.mkdir()
    return d / "cursor.json"


# ---- shape -----------------------------------------------------------------------------

def test_each_row_is_one_json_line_with_every_column(env, state):
    _decision(2, reason="line one\nline two")
    _audit_row()
    out = io.StringIO()
    counts = audit_export.export(_db(), state, out)
    assert counts == {"decisions": 2, "audit_log": 1}
    text = out.getvalue()
    assert text.isascii() and text.endswith("\n")
    lines = [json.loads(line) for line in text.splitlines()]
    assert len(lines) == 3                         # a newline in a value never splits a row
    by_table = {"decisions": _rows("decisions"), "audit_log": _rows("audit_log")}
    for entry in lines:
        table = entry["table"]
        assert entry["service"] == "audit"
        assert list(entry) == ["service", "table", *_columns(table)]
        stored = by_table[table].pop(0)
        assert {k: v for k, v in entry.items() if k not in ("service", "table")} == stored
    decision = lines[0]
    assert decision["hash"] and decision["prev_hash"] == decisions.GENESIS
    assert lines[1]["prev_hash"] == decision["hash"]          # the chain travels with it
    assert decision["grant_chain"] == "[]"                    # JSON columns stay JSON text
    assert decision["reason"] == "line one\nline two"


def test_no_secret_from_the_owners_flows_reaches_the_export(client, owner, admin_headers,
                                                            echo_local, state):
    """Like tests/targets/test_secrets_in_logs.py, for the export: drive the
    flows that handle secrets (login, a failed login, an admin token, an
    agent key and its use, a rotation), then export the whole record. No
    plaintext secret may appear in it."""
    from .conftest import CSRF_HEADERS
    wrong = "not-the-owner-password-0123"
    assert client.post("/auth/login", json={"username": owner.username,
                                            "password": wrong}).status_code == 401
    assert client.post("/auth/login", json={"username": owner.username,
                                            "password": owner.password}).status_code == 200
    token = client.post("/v1/admin/tokens", json={"name": "deploy"},
                        headers=admin_headers).json()["token"]
    made = client.post("/v1/admin/keys", headers=admin_headers, json={
        "name": "bot", "role": "full", "rate_per_min": 30,
        "capabilities": [cap(["post_item"], selector={"room": ["r1"]})]}).json()
    key = made["key"]
    client.post("/v1/targets/echo/actions/post_item",
                json={"params": {"room": "r1", "text": "private words"}},
                headers={"Authorization": f"Bearer {key}"})
    rotated = client.post(f"/v1/admin/keys/{made['id']}/rotate",
                          headers=admin_headers).json()["key"]
    client.post("/auth/logout", headers=CSRF_HEADERS)
    rows = _export(state)
    tables = {r["table"] for r in rows}
    assert tables == {"decisions", "audit_log"}
    text = json.dumps(rows)
    for secret in (owner.password, wrong, token, key, rotated, "private words",
                   admin_headers["Authorization"].split()[1]):
        if secret:
            assert secret not in text


def test_params_never_leave_only_their_hash(echo_local, make_agent, state):
    a = make_agent([cap(["post_item"])])
    engine.perform(a.auth, "echo", "post_item", {"room": "r1", "text": "very-private-text"})
    rows = _export(state)
    assert rows and all("params" not in r for r in rows)
    assert "very-private-text" not in json.dumps(rows)
    assert all(r["params_hash"] for r in rows if r["table"] == "decisions")


# ---- the cursor ------------------------------------------------------------------------

def test_the_cursor_moves_forward_and_no_row_is_exported_twice(env, state):
    _decision(3)
    _audit_row()
    first = _export(state)
    assert [(r["table"], r["id"]) for r in first] == [
        ("decisions", 1), ("decisions", 2), ("decisions", 3), ("audit_log", 1)]
    saved = json.loads(state.read_text())
    assert saved["version"] == 1
    assert saved["decisions"]["id"] == 3 and saved["audit_log"]["id"] == 1
    assert _export(state) == []                              # nothing new, nothing printed
    _decision(1)
    _audit_row()
    _audit_row()
    second = _export(state)
    assert [(r["table"], r["id"]) for r in second] == [
        ("decisions", 4), ("audit_log", 2), ("audit_log", 3)]
    seen = [(r["table"], r["id"]) for r in first + second]
    assert len(seen) == len(set(seen)) == 7


def test_batches_and_a_crash_mid_run_leave_no_gap(env, state, monkeypatch):
    monkeypatch.setattr(audit_export, "BATCH", 2)
    _decision(5)

    class Breaks(io.StringIO):
        writes = 0

        def write(self, s):
            Breaks.writes += 1
            if Breaks.writes == 2:                   # the second batch never gets out
                raise OSError("stdout gone")
            return super().write(s)

    out = Breaks()
    with pytest.raises(OSError):
        audit_export.export(_db(), state, out)
    assert [json.loads(x)["id"] for x in out.getvalue().splitlines()] == [1, 2]
    assert json.loads(state.read_text())["decisions"]["id"] == 2
    rest = _export(state)
    assert [r["id"] for r in rest] == [3, 4, 5]               # resumes after the batch it printed


def test_a_replaced_database_is_exported_again_from_the_start(env, state, caplog):
    _decision(3)
    _export(state)
    # The file is swapped for a fresh one (a restore, a re-install): its row 3
    # is not the row the cursor points at, so ids 1..3 must not be skipped.
    for suffix in ("", "-wal", "-shm"):
        Path(str(_db()) + suffix).unlink(missing_ok=True)
    db.init()
    _decision(3, resource="other@s.whatsapp.net")
    with caplog.at_level(logging.WARNING, logger="broker.audit_export"):
        again = _export(state)
    assert [r["id"] for r in again] == [1, 2, 3]
    assert all(r["resource"] == "other@s.whatsapp.net" for r in again)
    assert "does not match broker.db" in caplog.text and "row changed" in caplog.text


def test_a_shorter_replacement_is_caught_too(env, state, caplog):
    _decision(3)
    _export(state)
    for suffix in ("", "-wal", "-shm"):
        Path(str(_db()) + suffix).unlink(missing_ok=True)
    db.init()
    _decision(1)
    with caplog.at_level(logging.WARNING, logger="broker.audit_export"):
        assert [r["id"] for r in _export(state)] == [1]
    assert "row missing" in caplog.text


def test_a_replaced_database_is_reported_once_even_while_empty(env, state, caplog):
    _decision(2)
    _export(state)
    for suffix in ("", "-wal", "-shm"):
        Path(str(_db()) + suffix).unlink(missing_ok=True)
    db.init()                                        # the new decisions table is empty
    with caplog.at_level(logging.WARNING, logger="broker.audit_export"):
        assert _export(state) == []
        assert _export(state) == []
    assert caplog.text.count("does not match broker.db") == 1
    _decision(1)
    assert [r["id"] for r in _export(state)] == [1]


@pytest.mark.parametrize("content", ["not json", "[]", '{"version": 2}',
                                     '{"version": 1, "decisions": {"id": "3", "row": "x"}}',
                                     '{"version": 1, "decisions": {"id": -1, "row": "x"}}'])
def test_an_unreadable_cursor_restarts_the_export(env, state, caplog, content):
    _decision(2)
    state.write_text(content)
    with caplog.at_level(logging.WARNING, logger="broker.audit_export"):
        assert [r["id"] for r in _export(state)] == [1, 2]
    assert "cursor unreadable" in caplog.text
    assert json.loads(state.read_text())["decisions"]["id"] == 2


def test_reset_exports_everything_again(env, state):
    _decision(2)
    _export(state)
    assert [r["id"] for r in _export(state, reset=True)] == [1, 2]
    assert _export(state) == []


def test_a_cursor_that_cannot_be_written_is_an_export_error(env, tmp_path):
    _decision(1)
    with pytest.raises(audit_export.ExportError, match="cannot write the cursor"):
        audit_export.export(_db(), tmp_path / "missing-dir" / "cursor.json", io.StringIO())


# ---- read-only ---------------------------------------------------------------------------

def test_the_connection_is_read_only(env):
    _decision(1)
    before = _db().read_bytes()
    conn = audit_export.open_readonly(_db())
    try:
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly|read-only|query_only"):
            conn.execute("INSERT INTO app_config (key, value) VALUES ('x', 'y')")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM decisions")
        conn.execute("PRAGMA query_only = OFF")          # even switched off, mode=ro holds
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM decisions")
    finally:
        conn.close()
    assert _db().read_bytes() == before
    assert len(_rows("decisions")) == 1


def test_a_missing_database_is_never_created(env, tmp_path, state):
    missing = tmp_path / "nowhere" / "broker.db"
    with pytest.raises(audit_export.ExportError, match="cannot be opened read-only"):
        audit_export.export(missing, state, io.StringIO())
    assert not missing.exists() and not missing.parent.exists()
    assert not state.exists()


def test_it_reads_while_the_broker_holds_the_database_open(env, state):
    """In production the broker keeps one connection open (db.hold_open), so
    -wal and -shm exist for the exporter; rows committed by other
    connections meanwhile are visible to it."""
    keeper = db.hold_open()
    try:
        _decision(2)
        assert [r["id"] for r in _export(state)] == [1, 2]
        _decision(1)
        assert [r["id"] for r in _export(state)] == [3]
    finally:
        keeper.close()


# ---- malformed input ---------------------------------------------------------------------

def _broken(tmp_path, kind: str) -> Path:
    path = tmp_path / f"{kind}.db"
    if kind == "empty":
        path.write_bytes(b"")
    elif kind == "garbage":
        path.write_bytes(b"this is not an sqlite database at all" * 200)
    elif kind == "one_table":
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE decisions (id INTEGER PRIMARY KEY, hash TEXT)")
        conn.execute("INSERT INTO decisions (hash) VALUES ('h')")
        conn.commit()
        conn.close()
    elif kind == "reserved_column":
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE decisions (id INTEGER PRIMARY KEY, service TEXT)")
        conn.execute("CREATE TABLE audit_log (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()
    return path


@pytest.mark.parametrize("kind,message", [
    ("empty", "no decisions table"),
    ("garbage", "could not be read"),
    ("one_table", "no audit_log table"),
    ("reserved_column", "column named service"),
])
def test_a_malformed_database_exports_nothing_and_moves_nothing(env, tmp_path, state, kind,
                                                                message, caplog):
    path = _broken(tmp_path, kind)
    out = io.StringIO()
    with pytest.raises(audit_export.ExportError, match=message):
        audit_export.export(path, state, out)
    assert out.getvalue() == ""                      # not even the table that was fine
    assert not state.exists()
    with caplog.at_level(logging.WARNING, logger="broker.audit_export"):
        assert audit_export.run_once(path, state, out) is False
    assert "audit export failed" in caplog.text and out.getvalue() == ""


def test_an_empty_record_exports_nothing_and_succeeds(env, state):
    assert _export(state) == []
    assert audit_export.run_once(_db(), state, io.StringIO()) is True


# ---- hashing -----------------------------------------------------------------------------

DIGEST = re.compile(r"^sha256:[0-9a-f]{16}$")


def test_hash_resources_replaces_every_resource_id(env, state):
    _decision(1)
    _decision(1, resource="")
    _audit_row({"reason": "family", "count": 3, "chat_id": RESOURCE,
                "capabilities": [{"target": "whatsapp", "resources": {"chat": [RESOURCE]}}]})
    rows = _export(state, hash_resources=True)
    text = json.dumps(rows)
    assert RESOURCE not in text and "family" not in text
    first, empty, audit_entry = rows
    expected = "sha256:" + hashlib.sha256(RESOURCE.encode()).hexdigest()[:16]
    assert first["resource"] == expected and DIGEST.match(first["resource"])
    assert empty["resource"] == ""                    # nothing to hide
    assert DIGEST.match(audit_entry["resource"])
    detail = json.loads(audit_entry["detail"])
    assert detail["count"] == 3                       # numbers and keys stay
    assert detail["chat_id"] == expected              # the same id hashes the same way
    assert detail["capabilities"][0]["resources"]["chat"] == [expected]
    assert DIGEST.match(detail["reason"])
    # Everything that is not an identifier is untouched.
    assert (first["action"], first["key_name"], first["reason"]) == (
        "send_message", "bot", "out_of_grant")
    assert first["hash"] == _rows("decisions")[0]["hash"]


def test_a_detail_that_is_not_json_is_hashed_whole(env, state):
    with db.connect() as conn:
        conn.execute("INSERT INTO audit_log (ts, actor, action, resource, detail) VALUES "
                     "(1, 'system', 'odd', '', ?)", (f"raw {RESOURCE}",))
    [row] = _export(state, hash_resources=True)
    assert DIGEST.match(row["detail"])


def test_without_the_option_ids_are_exported_as_stored(env, state):
    _decision(1)
    assert _export(state)[0]["resource"] == RESOURCE


# ---- logging -------------------------------------------------------------------------------

def test_the_exporters_own_lines_never_carry_row_content(env, state, caplog):
    _decision(2)
    _audit_row()
    with caplog.at_level(logging.DEBUG):
        assert audit_export.run_once(_db(), state, io.StringIO()) is True
    text = caplog.text
    assert "audit export done" in text and "decisions=2" in text and "audit_log=1" in text
    for value in (RESOURCE, "family", "private words", _rows("decisions")[0]["hash"]):
        assert value not in text


# ---- the loop --------------------------------------------------------------------------------

def test_the_loop_runs_until_stopped_and_survives_failures(caplog):
    stop = threading.Event()
    calls = []

    def run(first):
        calls.append(first)
        if len(calls) == 2:
            raise RuntimeError("boom with secret-ish detail")
        if len(calls) == 4:
            stop.set()
        return len(calls) != 3

    with caplog.at_level(logging.ERROR, logger="broker.audit_export"):
        audit_export.run_loop(run, 0.001, stop)
    assert calls == [True, False, False, False]
    assert "audit export crashed" in caplog.text and "RuntimeError" in caplog.text
    assert "secret-ish" not in caplog.text


# ---- the CLI ---------------------------------------------------------------------------------

def test_cli_export_prints_rows_and_resumes(env, state, capsys, logs_to_stderr):
    from cli.aab import main
    _decision(2)
    assert main(["audit", "export", "--state", str(state), "--db", str(_db())]) == 0
    out = capsys.readouterr().out
    assert [json.loads(x)["id"] for x in out.splitlines()] == [1, 2]
    assert main(["audit", "export", "--state", str(state)]) == 0      # --db from BROKER_DB
    assert capsys.readouterr().out == ""


def test_cli_reads_its_defaults_from_the_environment(env, state, capsys, monkeypatch,
                                                     logs_to_stderr):
    from cli.aab import main
    _decision(1)
    monkeypatch.setenv("AUDIT_EXPORT_STATE", str(state))
    monkeypatch.setenv("AUDIT_EXPORT_HASH_RESOURCES", "true")
    assert main(["audit", "export"]) == 0
    [row] = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert DIGEST.match(row["resource"])


@pytest.mark.parametrize("value", ["TRUE", "yes", "1", "on"])
def test_cli_accepts_the_usual_true_spellings(env, state, capsys, monkeypatch, value,
                                              logs_to_stderr):
    from cli.aab import main
    _decision(1)
    monkeypatch.setenv("AUDIT_EXPORT_HASH_RESOURCES", value)
    assert main(["audit", "export", "--state", str(state)]) == 0
    assert RESOURCE not in capsys.readouterr().out


def test_cli_refuses_an_unreadable_privacy_switch(env, state, capsys, monkeypatch,
                                                  logs_to_stderr):
    from cli.aab import main
    _decision(1)
    monkeypatch.setenv("AUDIT_EXPORT_HASH_RESOURCES", "hash-please")
    assert main(["audit", "export", "--state", str(state)]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "true or false" in captured.err
    assert not state.exists()


def test_cli_needs_a_cursor_path(env, capsys, monkeypatch, logs_to_stderr):
    from cli.aab import main
    monkeypatch.delenv("AUDIT_EXPORT_STATE", raising=False)
    assert main(["audit", "export"]) == 2
    assert "AUDIT_EXPORT_STATE" in capsys.readouterr().err


def test_cli_exits_non_zero_when_the_database_is_unreadable(env, tmp_path, state, capsys,
                                                            logs_to_stderr):
    from cli.aab import main
    assert main(["audit", "export", "--state", str(state),
                 "--db", str(tmp_path / "missing.db")]) == 1
    assert capsys.readouterr().out == ""


def test_cli_loop_takes_its_interval_from_the_environment(env, state, monkeypatch,
                                                          logs_to_stderr):
    from cli import aab
    seen = {}

    def fake_loop(run, interval, stop):
        seen["interval"] = interval
        seen["first"] = run(True)

    monkeypatch.setattr(audit_export, "run_loop", fake_loop)
    monkeypatch.setattr("signal.signal", lambda *a: None)
    monkeypatch.setenv("AUDIT_EXPORT_INTERVAL", "120")
    assert aab.main(["audit", "export", "--state", str(state), "--loop"]) == 0
    assert seen == {"interval": 120, "first": True}
    monkeypatch.delenv("AUDIT_EXPORT_INTERVAL")
    assert aab.main(["audit", "export", "--state", str(state), "--loop"]) == 0
    assert seen["interval"] == 3600
    for bad in ("0", "-5", "hourly"):
        monkeypatch.setenv("AUDIT_EXPORT_INTERVAL", bad)
        assert aab.main(["audit", "export", "--state", str(state), "--loop"]) == 2


def test_cli_audit_lines_name_the_audit_exporter(env, state, monkeypatch):
    """Its own log lines say service=audit-exporter (configure() is a no-op
    in this test process, so the name it would pass is checked instead)."""
    from cli import aab
    names = []
    monkeypatch.setattr(aab, "_log_to_stderr", lambda service="aab-cli": names.append(service))
    aab.main(["audit", "export", "--state", str(state)])
    aab.main(["skill", "build", "--all-plugins", "--out", str(state.parent / "s.md")])
    assert names == ["audit-exporter", "aab-cli"]


def test_saving_the_cursor_leaves_no_temporary_file(tmp_path):
    d = tmp_path / "s"
    d.mkdir()
    audit_export.save_state(d / "cursor.json", {"decisions": {"id": 1, "row": "x"}})
    audit_export.save_state(d / "cursor.json", {"decisions": {"id": 2, "row": "y"}})
    assert sorted(p.name for p in d.iterdir()) == ["cursor.json"]
    assert audit_export.load_state(d / "cursor.json") == {"decisions": {"id": 2, "row": "y"}}
