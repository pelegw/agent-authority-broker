"""The owner's health summary for monitoring: `GET /v1/admin/health`.

`/health` answers liveness to anyone, so it must stay blind to plugin and
channel state (routers/health.py). An uptime monitor needs the opposite: one
authenticated call that says whether the broker can still do its job, as an
HTTP status it can alert on (200 ok, 503 degraded). This module builds it.

The checks are live, not read back from stored state: nothing refreshes a
plugin's health on its own, so a summary built from `last_health` would stay
green after a container died. Every enabled plugin's `/status` is fetched
and stored exactly as `POST /v1/admin/plugins/{id}/health` stores it, so the
console's cards are current too. The fetches run at once, so the call takes
one plugin timeout at most, not one per plugin. A disabled plugin is never a
failure (the owner switched it off); an enabled one must be reachable,
healthy and connected. Telegram counts only when it is enabled, and an
enabled channel that could not deliver an approval card right now is a
failure. Nothing here is audited: a monitor asking every minute is not an
owner action.
"""

import contextvars
import logging
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

from .. import __version__, db
from ..logging_setup import kv
from ..notify import telegram
from ..plugins.registry import get_registry, plugin_rows
from . import plugins_admin

log = logging.getLogger(__name__)

# The poll loop retries on its own, so one failed getUpdates is a blip. A
# streak means cards are not arriving; three in a row is a sustained outage.
TELEGRAM_ERROR_STREAK = 3


def summary() -> dict:
    """Run every check and fold them into one report (see the module doc)."""
    checks = {"database": _database(), "plugins": _plugins(), "telegram": _telegram()}
    failing = []
    if not checks["database"]["ok"]:
        failing.append("database")
    failing += [f"plugins.{pid}" for pid, c in checks["plugins"].items() if not c["ok"]]
    if not checks["telegram"]["ok"]:
        failing.append("telegram")
    return {"status": "ok" if not failing else "degraded", "version": __version__,
            "checked_at": int(time.time()), "checks": checks, "failing": failing}


def _database() -> dict:
    try:
        conn = db.connect()
        try:
            conn.execute("SELECT 1").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        log.error("database check failed %s", kv(error=type(exc).__name__))
        return {"ok": False, "error": type(exc).__name__}
    return {"ok": True}


def _plugins() -> dict:
    reg = get_registry()
    enabled = reg.enabled_plugins()
    if enabled:
        # Each refresh runs under a copy of the request's context, so the
        # request id reaches the plugin's log line as it does for every other
        # admin call; refresh_health() turns an adapter failure into a stored
        # unhealthy record rather than raising.
        with ThreadPoolExecutor(max_workers=len(enabled)) as pool:
            futures = [pool.submit(contextvars.copy_context().run,
                                   plugins_admin.refresh_health, pid) for pid in enabled]
            for future in futures:
                future.result()
    rows = plugin_rows()
    out = {}
    for pid in sorted(reg.entries()):
        row = rows.get(pid, {})
        entry: dict = {"ok": True, "enabled": row.get("enabled") == 1}
        if entry["enabled"]:
            last = row.get("last_health") or {}
            healthy = last.get("healthy") is True
            connected = row.get("connected") == 1
            entry.update(ok=healthy and connected, healthy=healthy, connected=connected)
            # The plugin's own words ("waiting for QR pairing") or the refresh
            # failure ("plugin service unreachable"): what the alert should say.
            for key in ("health", "error"):
                if last.get(key):
                    entry[key] = last[key]
        out[pid] = entry
    return out


def _telegram() -> dict:
    entry: dict = {"ok": True, "enabled": telegram.enabled()}
    if not entry["enabled"]:
        return entry
    token, linked, poll = telegram.token_state(), telegram.linked(), telegram.poll_state()
    streak = int(poll.get("consecutive_errors") or 0)
    problems = []
    if token != "set":
        problems.append(f"token {token}")          # unset | unreadable
    if not linked:
        problems.append("chat not linked")
    if not poll.get("running"):
        problems.append("poll loop not running")
    if streak >= TELEGRAM_ERROR_STREAK:
        problems.append(f"{streak} poll errors in a row")
    entry.update(ok=not problems, token=token, linked=linked,
                 poll_running=bool(poll.get("running")), consecutive_errors=streak,
                 last_ok_at=poll.get("last_ok_at"))
    if problems:
        entry["reason"] = "; ".join(problems)
    return entry
