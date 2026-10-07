"""Export the audit record as JSON lines on stdout, for a log shipper.

The audit record is two append-only tables in broker.db: `decisions` (the
hash-chained decision record) and `audit_log` (human and system actions).
`aab audit export` prints every row it has not printed before, one JSON object
per line:

    {"service":"audit","table":"decisions","id":42,"request_id":"...", ...,
     "prev_hash":"...","hash":"...","signed":1}

Every column of the row is there, under its own name, with its stored value
(the JSON columns stay JSON text; the hash chain covers their parsed values,
decisions.py), except the free text inside `audit_log.detail` (below). In
the New Relic overlay (docker-compose.newrelic.yml) the `audit-exporter`
service runs this every hour; its stdout goes through the same Docker log
driver as every other service, so the rows reach New Relic as log events.
The exporter holds no New Relic credential and needs no network.

Why it is safe to ship:
  * broker.db is opened read-only twice over: the volume is mounted
    read-only, and the connection is `mode=ro` with `query_only`. The
    exporter cannot change the record it copies.
  * No params leave: decisions store only `params_hash`, and the `actions`
    table (the one that holds params, notes and labels) is never read.
  * No secret leaves: secrets live in `plugin_secrets` (never read) and the
    plugin services, never in these two tables.
  * No typed text leaves: in `audit_log.detail` the value of every
    free-text key (FREE_TEXT_KEYS: the owner's reason for hiding a
    resource, and any note, label, message or text a caller records)
    becomes `redacted:sha256:<16 hex>`, always. A detail that is not the
    JSON object audit() writes is redacted whole. Structural keys (ids,
    counts, flags, field names, scopes) stay.
  * With hash_resources (AUDIT_EXPORT_HASH_RESOURCES=true) every `resource`
    value, and every other string inside `audit_log.detail`, becomes
    `sha256:<16 hex>`. Identifiers then stay joinable but not readable. It is
    an unkeyed hash: a short identifier (a phone number) can be guessed back.

Where it resumes: a small JSON cursor file in the exporter's own volume holds,
per table, the last exported id and a digest of that row's identity columns
(IDENTITY). It cannot live in app_config, because the exporter cannot write
broker.db (above). A row is printed before the cursor moves past it, so a
crash between the two prints that batch again on the next run (at least once,
never a gap). If the row under the cursor is gone or different, broker.db was
replaced or restored: the exporter logs a warning and exports that table
again from the start.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import sqlite3
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

from .logging_setup import kv

log = logging.getLogger(__name__)

TABLES = ("decisions", "audit_log")
# Keys the exporter adds to every line; no column may use them.
RESERVED = ("service", "table")
SERVICE = "audit"
BATCH = 500
BUSY_TIMEOUT_SECONDS = 10.0
STATE_VERSION = 1
HASH_PREFIX = "sha256:"
HASH_HEX_CHARS = 16
# The columns that say which row the cursor points at, and nothing more. A
# digest of the whole row would change when an additive migration
# (db._MIGRATIONS, the upgrade path) adds a column, and the exporter would
# then take every table for a replaced database and export it again from
# id 1. A decision's hash already commits to its content (the chain); an
# audit_log row has no hash, so its id, time, actor and action stand in.
IDENTITY = {"decisions": ("id", "hash"), "audit_log": ("id", "ts", "actor", "action")}
# audit_log.detail keys whose value is text a person or an agent typed. Today
# that is hidden.add's `reason` (the owner's words, services/admin.py); the
# others are reserved for the same kind of value, so a future caller that
# records a note or a label cannot ship it by accident. Matched at any depth.
FREE_TEXT_KEYS = frozenset({"reason", "note", "label", "message", "text"})
REDACTED = "redacted:"


class ExportError(Exception):
    """A run that exported nothing more. The message names the cause only;
    it never carries row content."""


# ---- reading broker.db ---------------------------------------------------------------

def open_readonly(db_path: str | os.PathLike) -> sqlite3.Connection:
    """broker.db through a read-only URI. `mode=ro` never creates a missing
    file and refuses every write; query_only refuses them again in SQLite's
    own layer, in case a future caller forgets which connection it holds."""
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT_SECONDS)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
    except sqlite3.Error as exc:
        raise ExportError(f"broker.db cannot be opened read-only ({exc}); is the broker "
                          "running?") from exc
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
    if not cols:
        raise ExportError(f"broker.db has no {table} table")
    clash = sorted(set(cols) & set(RESERVED))
    if clash:
        raise ExportError(f"{table} has a column named {', '.join(clash)}, which the export "
                          "uses for itself")
    missing = [c for c in IDENTITY[table] if c not in cols]
    if missing:
        raise ExportError(f"{table} has no {', '.join(missing)} "
                          f"column{'s' if len(missing) > 1 else ''}")
    return cols


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=True,
                      separators=(",", ":")).encode("ascii")


def _fingerprint(table: str, row: sqlite3.Row) -> str:
    """A digest of the row's identity columns, to recognise it on the next run."""
    return hashlib.sha256(_canonical([row[c] for c in IDENTITY[table]])).hexdigest()


def _legacy_fingerprint(row: sqlite3.Row) -> str:
    """The first release's digest, of every column. A cursor saved by it is
    still recognised (only an unchanged row matches), so upgrading does not
    export everything again; the next save writes the identity digest."""
    return hashlib.sha256(_canonical(dict(row))).hexdigest()


# ---- the line -----------------------------------------------------------------------------

def digest(value: str) -> str:
    return HASH_PREFIX + hashlib.sha256(value.encode("utf-8")).hexdigest()[:HASH_HEX_CHARS]


def redacted(value):
    """The marker that replaces typed text: equal texts give equal markers,
    so they can still be counted, but the words stay on this server. None
    and "" stay as they are (nothing was typed)."""
    if value is None or value == "":
        return value
    text = value if isinstance(value, str) else _canonical(value).decode("ascii")
    return REDACTED + digest(text)


def _clean(value, hash_strings: bool):
    if isinstance(value, dict):
        # Any spelling of a free-text key ("Reason" too): a miss here ships words.
        return {k: redacted(v) if k.lower() in FREE_TEXT_KEYS else _clean(v, hash_strings)
                for k, v in value.items()}
    if isinstance(value, list):
        return [_clean(v, hash_strings) for v in value]
    if hash_strings and isinstance(value, str) and value:
        return digest(value)
    return value


def export_detail(raw, *, hash_resources: bool = False):
    """audit_log.detail as it leaves the server. Free text is redacted
    whatever the option says. With hash_resources every other string is
    hashed too, keys kept: detail can quote identifiers (a hidden resource,
    a capability's resource list, a Telegram chat). A detail that is not the
    JSON object audit() writes is redacted whole: nothing in it can be
    trusted to be structural."""
    if raw is None or raw == "":
        return raw
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return redacted(str(raw))
    if not isinstance(parsed, dict):
        return redacted(str(raw))
    return json.dumps(_clean(parsed, hash_resources))


def line(table: str, row: sqlite3.Row, *, hash_resources: bool = False) -> str:
    """One row as one JSON line (ASCII-escaped: a value can never break it)."""
    entry = {"service": SERVICE, "table": table, **dict(row)}
    if table == "audit_log" and "detail" in entry:
        entry["detail"] = export_detail(entry["detail"], hash_resources=hash_resources)
    if hash_resources and isinstance(entry.get("resource"), str) and entry["resource"]:
        entry["resource"] = digest(entry["resource"])
    return json.dumps(entry, ensure_ascii=True, separators=(",", ":"))


# ---- the cursor --------------------------------------------------------------------------

def load_state(path: str | os.PathLike) -> dict:
    """{table: {"id": int, "row": digest}}; empty when there is no file yet.
    An unreadable file restarts the export (duplicates are visible and can be
    told apart by table, id and hash; a silent gap could not)."""
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if data.get("version") != STATE_VERSION:
            raise ValueError("version")
        out = {}
        for table in TABLES:
            entry = data.get(table)
            if entry is None:
                continue
            if not (isinstance(entry.get("id"), int) and entry["id"] >= 0
                    and isinstance(entry.get("row"), str)):
                raise ValueError(table)
            out[table] = {"id": entry["id"], "row": entry["row"]}
        return out
    except (OSError, ValueError, AttributeError):
        log.warning("audit export cursor unreadable; exporting from the start %s",
                    kv(state=str(p)))
        return {}


def save_state(path: str | os.PathLike, state: dict) -> None:
    """Atomically: a reader (the next run) sees the old cursor or the new one."""
    p = Path(path)
    body = json.dumps({"version": STATE_VERSION, **state}, sort_keys=True).encode("utf-8")
    try:
        fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".cursor.tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(body)
            os.replace(tmp, p)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
    except OSError as exc:
        raise ExportError(f"cannot write the cursor in {p.parent} ({type(exc).__name__})") \
            from exc


def _resume_from(conn: sqlite3.Connection, table: str, saved: dict | None) -> dict:
    """The cursor to export after: the saved one, unless the row it points
    at is no longer the row it exported (a replaced or restored broker.db).
    A cursor in the first release's form comes back in the current form."""
    if not saved or saved["id"] == 0:
        return {"id": 0, "row": ""}
    row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (saved["id"],)).fetchone()
    if row is not None:
        current = _fingerprint(table, row)
        if saved["row"] == current or saved["row"] == _legacy_fingerprint(row):
            return {"id": saved["id"], "row": current}
    log.warning("audit export cursor does not match broker.db; exporting the table from "
                "the start %s", kv(table=table, cursor=saved["id"],
                                   reason="row missing" if row is None else "row changed"))
    return {"id": 0, "row": ""}


# ---- one run, and the loop -----------------------------------------------------------------

def export(db_path: str | os.PathLike, state_path: str | os.PathLike, out: TextIO, *,
           hash_resources: bool = False, reset: bool = False) -> dict[str, int]:
    """Print every row not exported yet; returns {table: rows printed}."""
    state = {} if reset else load_state(state_path)
    counts = {}
    with contextlib.closing(open_readonly(db_path)) as conn:
        try:
            # Both tables are checked before either is read: a database
            # without one of them prints nothing, rather than one table on
            # every run.
            for table in TABLES:
                _columns(conn, table)
            for table in TABLES:
                saved = state.get(table)
                cursor = _resume_from(conn, table, saved)
                if saved and cursor != saved:
                    # Save a changed cursor at once: a stale one is then
                    # forgotten (its warning logged once, even while the new
                    # table is still empty), and a first-release one is
                    # rewritten before any migration can change its row.
                    state[table] = cursor
                    save_state(state_path, state)
                after = cursor["id"]
                counts[table] = 0
                while True:
                    rows = conn.execute(f"SELECT * FROM {table} WHERE id > ? ORDER BY id "
                                        "LIMIT ?", (after, BATCH)).fetchall()
                    if not rows:
                        break
                    out.write("".join(line(table, r, hash_resources=hash_resources) + "\n"
                                      for r in rows))
                    out.flush()
                    # Only after the batch is out: a crash here repeats it.
                    after = rows[-1]["id"]
                    state[table] = {"id": after, "row": _fingerprint(table, rows[-1])}
                    save_state(state_path, state)
                    counts[table] += len(rows)
        except sqlite3.Error as exc:
            # On a read-only mount this is what a stopped broker looks like:
            # its -wal and -shm files are gone, and this reader cannot make them.
            hint = "; is the broker running?" if "unable to open" in str(exc) else ""
            raise ExportError(f"broker.db could not be read ({exc}){hint}") from exc
    return counts


def run_once(db_path, state_path, out: TextIO, *, hash_resources: bool = False,
             reset: bool = False) -> bool:
    """One run with its outcome logged; True when it succeeded."""
    try:
        counts = export(db_path, state_path, out, hash_resources=hash_resources, reset=reset)
    except ExportError as exc:
        log.warning("audit export failed; nothing more exported this run %s",
                    kv(reason=str(exc)))
        return False
    except OSError as exc:                            # stdout closed or failing
        log.warning("audit export failed writing its output %s", kv(error=type(exc).__name__))
        return False
    log.info("audit export done %s", kv(hash_resources=hash_resources, **counts))
    return True


class StopFlag:
    """Set by a signal handler, read by run_loop.

    Why not threading.Event: a Python signal handler runs on the main thread
    between two bytecodes, possibly while that same thread is inside
    Event.wait() holding the Event's internal lock. Event.set() takes that
    lock too, and the lock is not reentrant, so `docker stop` could hang the
    exporter until it is killed. Setting a plain attribute takes no lock."""

    def __init__(self) -> None:
        self.stopped = False

    def set(self, *_signal_args) -> None:
        """Usable as a signal handler directly: signal.signal(sig, flag.set)."""
        self.stopped = True


# How long the loop sleeps between two looks at the stop flag: the most a
# `docker stop` waits for the exporter to exit (when no run is in progress).
STOP_CHECK_SECONDS = 1.0


def run_loop(run: Callable[[], bool], interval: float, stop: StopFlag, *,
             sleep: Callable[[float], None] = time.sleep,
             clock: Callable[[], float] = time.monotonic) -> None:
    """Call `run()` now and then every `interval` seconds until `stop` is
    set. A failed run (broker stopped, database replaced mid-read) is logged
    by `run` and retried at the next interval; the loop never exits on one.
    An unexpected exception is logged by class only and retried too."""
    while not stop.stopped:
        try:
            run()
        except Exception as exc:                      # never let one bad run end the service
            log.error("audit export crashed; retrying next run %s",
                      kv(error=type(exc).__name__))
        # Short naps, not one long one: the flag is looked at every second.
        deadline = clock() + interval
        while not stop.stopped:
            left = deadline - clock()
            if left <= 0:
                break
            sleep(min(STOP_CHECK_SECONDS, left))
