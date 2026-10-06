"""Export the audit record as JSON lines on stdout, for a log shipper.

The audit record is two append-only tables in broker.db: `decisions` (the
hash-chained decision record) and `audit_log` (human and system actions).
`aab audit export` prints every row it has not printed before, one JSON object
per line:

    {"service":"audit","table":"decisions","id":42,"request_id":"...", ...,
     "prev_hash":"...","hash":"...","signed":1}

Every column of the row is there, under its own name, with its stored value
(the JSON columns stay JSON text; the hash chain covers their parsed values,
decisions.py). In the New Relic overlay (docker-compose.newrelic.yml) the
`audit-exporter` service runs this every hour; its stdout goes through the
same Docker log driver as every other service, so the rows reach New Relic as
log events. The exporter holds no New Relic credential and needs no network.

Why it is safe to ship:
  * broker.db is opened read-only twice over: the volume is mounted
    read-only, and the connection is `mode=ro` with `query_only`. The
    exporter cannot change the record it copies.
  * No params leave: decisions store only `params_hash`, and the `actions`
    table (the one that holds params, notes and labels) is never read.
  * No secret leaves: secrets live in `plugin_secrets` (never read) and the
    plugin services, never in these two tables.
  * With hash_resources (AUDIT_EXPORT_HASH_RESOURCES=true) every `resource`
    value, and every string inside `audit_log.detail`, becomes
    `sha256:<16 hex>`. Identifiers then stay joinable but not readable. It is
    an unkeyed hash: a short identifier (a phone number) can be guessed back.

Where it resumes: a small JSON cursor file in the exporter's own volume holds,
per table, the last exported id and a digest of that row. It cannot live in
app_config, because the exporter cannot write broker.db (above). A row is
printed before the cursor moves past it, so a crash between the two prints
that batch again on the next run (at least once, never a gap). If the row
under the cursor is gone or different, broker.db was replaced or restored:
the exporter logs a warning and exports that table again from the start.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import sqlite3
import tempfile
import threading
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
    return cols


def _fingerprint(row: sqlite3.Row) -> str:
    """A digest of the raw row, to recognise it again on the next run."""
    text = json.dumps(dict(row), sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("ascii")).hexdigest()


# ---- the line -----------------------------------------------------------------------------

def digest(value: str) -> str:
    return HASH_PREFIX + hashlib.sha256(value.encode("utf-8")).hexdigest()[:HASH_HEX_CHARS]


def _hash_strings(value):
    if isinstance(value, str):
        return digest(value) if value else value
    if isinstance(value, list):
        return [_hash_strings(v) for v in value]
    if isinstance(value, dict):
        return {k: _hash_strings(v) for k, v in value.items()}
    return value


def _hash_resources(table: str, entry: dict) -> dict:
    if isinstance(entry.get("resource"), str) and entry["resource"]:
        entry["resource"] = digest(entry["resource"])
    if table == "audit_log" and entry.get("detail"):
        # detail is free-form JSON a caller wrote; it can quote identifiers
        # (a hidden resource, a capability's resource list, a Telegram chat).
        # Every string in it is hashed, keys kept; text that does not parse is
        # hashed whole, so nothing unexpected leaves in the clear.
        try:
            entry["detail"] = json.dumps(_hash_strings(json.loads(entry["detail"])))
        except (TypeError, ValueError):
            entry["detail"] = digest(str(entry["detail"]))
    return entry


def line(table: str, row: sqlite3.Row, *, hash_resources: bool = False) -> str:
    """One row as one JSON line (ASCII-escaped: a value can never break it)."""
    entry = {"service": SERVICE, "table": table, **dict(row)}
    if hash_resources:
        entry = _hash_resources(table, entry)
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


def _resume_from(conn: sqlite3.Connection, table: str, saved: dict | None) -> int:
    """The id to export after: the cursor, unless the row it points at is
    no longer the row it exported (a replaced or restored broker.db)."""
    if not saved or saved["id"] == 0:
        return 0
    row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (saved["id"],)).fetchone()
    if row is not None and _fingerprint(row) == saved["row"]:
        return saved["id"]
    log.warning("audit export cursor does not match broker.db; exporting the table from "
                "the start %s", kv(table=table, cursor=saved["id"],
                                   reason="row missing" if row is None else "row changed"))
    return 0


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
                after = _resume_from(conn, table, saved)
                if saved and after != saved["id"]:
                    # Forget the stale cursor at once, so the warning is
                    # logged once even while the new table is still empty.
                    state[table] = {"id": 0, "row": ""}
                    save_state(state_path, state)
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
                    state[table] = {"id": after, "row": _fingerprint(rows[-1])}
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


def run_loop(run: Callable[[bool], bool], interval: float, stop: threading.Event) -> None:
    """Call `run(first)` now and then every `interval` seconds until `stop`
    is set. A failed run (broker stopped, database replaced mid-read) is
    logged by `run` and retried at the next interval; the loop never exits on
    one. An unexpected exception is logged by class only and retried too."""
    first = True
    while not stop.is_set():
        try:
            run(first)
        except Exception as exc:                      # never let one bad run end the service
            log.error("audit export crashed; retrying next run %s",
                      kv(error=type(exc).__name__))
        first = False
        stop.wait(interval)
