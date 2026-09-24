"""Effective operator settings: `Settings` defaults overlaid by console edits.

The configuration principle (docs/configuration.md): files hold only what
cannot live in the database. Bootstrap secrets and fail-closed exposure
settings stay env-only; every other operator knob is edited from the console
and stored in `app_config` as `setting:<name>` (JSON). `runtime_settings()`
returns the effective values; env values, when present, remain the defaults
a console edit overrides and a reset (null) returns to.

Every setting is typed and bounded: a console edit outside its range is
refused with 400, and a stored row that no longer validates (hand-edited,
or bounds tightened in a later release) is ignored in favour of the env
default rather than trusted.

`mcp_allowed_hosts_extra` is additive by construction: the effective
`mcp_allowed_hosts` is the env list (MCP_ALLOWED_HOSTS) followed by the
console extras, so a hijacked console session can add a host but can never
remove `localhost` or anything else the operator put in the file.

Read on every call (one small SQLite read, no cache): with a single worker
there is nothing to invalidate, and a console edit takes effect on the
next request.
"""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass
from typing import Any

from . import db
from .audit import audit
from .config import get_settings
from .errors import PolicyError

PREFIX = "setting:"
MAX_EXTRA_HOSTS = 32

# host[:port|:*], where host is a DNS name, an IPv4 address or a bracketed
# IPv6 literal: the shapes the MCP transport's Host check understands.
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_HOST_RE = re.compile(r"^(?:\[[0-9A-Fa-f:.]+\]|" + _LABEL + r"(?:\." + _LABEL + r")*)"
                      r"(?::(?:\*|[0-9]{1,5}))?$")


@dataclass(frozen=True)
class Spec:
    """One console-editable setting: its type, bounds and help text."""
    name: str
    kind: str                      # int | float | hosts
    lo: float | None
    hi: float | None
    unit: str
    help: str

    def validate(self, value: Any) -> Any:
        """The normalized value, or ValueError saying what is wrong."""
        if self.kind == "hosts":
            return _validate_hosts(value)
        # bool is an int subclass in Python; `true` is never a number here.
        if isinstance(value, bool):
            raise ValueError(f"{self.name} must be a number")
        if self.kind == "int":
            if not isinstance(value, int):
                raise ValueError(f"{self.name} must be an integer")
        elif not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{self.name} must be a number")
        if not self.lo <= value <= self.hi:
            raise ValueError(f"{self.name} must be between {_fmt(self.lo)} and "
                             f"{_fmt(self.hi)} {self.unit}".rstrip())
        return float(value) if self.kind == "float" else int(value)


def _fmt(n: float) -> str:
    return str(int(n)) if float(n).is_integer() else str(n)


def _validate_hosts(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError("mcp_allowed_hosts_extra must be a list of host patterns")
    if len(value) > MAX_EXTRA_HOSTS:
        raise ValueError(f"at most {MAX_EXTRA_HOSTS} extra hosts")
    out: list[str] = []
    for h in value:
        if not isinstance(h, str) or len(h) > 262 or not _HOST_RE.match(h.strip()):
            raise ValueError("each extra host must look like host, host:port or host:*")
        host = h.strip().lower()
        port = host.rsplit(":", 1)[1] if ":" in host and not host.endswith("]") else None
        if port and port != "*" and not 0 < int(port) <= 65535:
            raise ValueError("port out of range")
        if host not in out:
            out.append(host)
    return tuple(out)


# Order here is the order the console shows. Bounds are deliberately wide
# enough for any sane deployment and narrow enough that a typo cannot make
# the broker unusable (a 0-second session, a scheduler that never ticks).
SPECS: dict[str, Spec] = {s.name: s for s in (
    Spec("max_delegation_depth", "int", 0, 10, "hops",
         "How many delegation hops below a root key are allowed (0 = no delegation)."),
    Spec("session_idle_seconds", "int", 300, 7 * 86400, "seconds",
         "Console sessions end after this much inactivity."),
    Spec("session_absolute_seconds", "int", 3600, 30 * 86400, "seconds",
         "Console sessions end this long after login regardless of activity "
         "(applies to sessions started after the change)."),
    Spec("key_rotation_grace_seconds", "int", 0, 30 * 86400, "seconds",
         "After a key rotation, the previous secret keeps working this long."),
    Spec("grant_max_hours", "int", 1, 365 * 24, "hours",
         "Longest expiry a permission request may ask for."),
    Spec("draft_ttl_hours", "int", 1, 30 * 24, "hours",
         "Drafts nobody decides on expire after this long."),
    Spec("scheduler_tick_seconds", "int", 1, 300, "seconds",
         "How often the scheduler looks for due queued actions."),
    Spec("schedule_min_lead_seconds", "int", 0, 3600, "seconds",
         "A scheduled action must be at least this far in the future."),
    Spec("schedule_max_horizon_days", "int", 1, 365, "days",
         "A scheduled action may be at most this far in the future."),
    Spec("long_poll_max_wait_seconds", "int", 0, 120, "seconds",
         "Longest ?wait= a long-poll read may hold a request open."),
    Spec("long_poll_interval_seconds", "float", 0.05, 10, "seconds",
         "How often a held long-poll read checks for something new."),
    Spec("plugin_timeout_seconds", "float", 1, 300, "seconds",
         "Plugin API calls give up after this long (a timeout after sending is "
         "an unknown outcome, never retried automatically)."),
    Spec("ancestors_cache_seconds", "int", 0, 3600, "seconds",
         "How long folder-ancestry answers (subtree narrowing) are cached."),
    Spec("mcp_allowed_hosts_extra", "hosts", None, None, "",
         "Extra Host headers the /mcp endpoint accepts, added to MCP_ALLOWED_HOSTS "
         "from the file (which cannot be removed from here)."),
)}


@dataclass(frozen=True)
class RuntimeSettings:
    max_delegation_depth: int
    session_idle_seconds: int
    session_absolute_seconds: int
    key_rotation_grace_seconds: int
    grant_max_hours: int
    draft_ttl_hours: int
    scheduler_tick_seconds: int
    schedule_min_lead_seconds: int
    schedule_max_horizon_days: int
    long_poll_max_wait_seconds: int
    long_poll_interval_seconds: float
    plugin_timeout_seconds: float
    ancestors_cache_seconds: int
    mcp_allowed_hosts_extra: tuple[str, ...]
    # Effective Host allowlist: env entries first (never removable), then extras.
    mcp_allowed_hosts: tuple[str, ...]


# Env-only keys and why each cannot move to the console. The console's
# "What lives in files" panel renders this; a test checks that every
# Settings field is either here or in SPECS, so no knob is hidden.
ENV_ONLY: tuple[dict, ...] = (
    {"name": "SETUP_TOKEN", "field": "setup_token", "secret": True, "category": "bootstrap",
     "why": "Authorizes creating the owner account; it must exist before anyone can log in."},
    {"name": "BROKER_SECRETS_KEY", "field": "broker_secrets_key", "secret": True,
     "category": "bootstrap",
     "why": "Decrypts the secrets entered in the console; it cannot be stored beside them."},
    {"name": "DECISION_SIGNING_KEY", "field": "decision_signing_key", "secret": True,
     "category": "bootstrap",
     "why": "Signs the decision record; a key kept in the database could re-sign a "
            "tampered chain."},
    {"name": "ORIGIN_SECRET", "field": "origin_secret", "secret": True, "category": "exposure",
     "why": "Origin lockdown behind Cloudflare; checked before any login exists, and must "
            "not be removable from a hijacked session."},
    {"name": "ORIGIN_SECRET_HEADER", "field": "origin_secret_header", "secret": False,
     "category": "exposure",
     "why": "Paired with ORIGIN_SECRET and the Cloudflare Transform Rule."},
    {"name": "TRUST_CF_CONNECTING_IP", "field": "trust_cf_connecting_ip", "secret": False,
     "category": "exposure",
     "why": "Decides which client IP rate limits see; a session must not be able to spoof it."},
    {"name": "CF_ACCESS_ENABLED", "field": "cf_access_enabled", "secret": False,
     "category": "exposure",
     "why": "Cloudflare Access on the admin plane; boot fails closed without it in public "
            "mode, so a hijacked session must not be able to switch it off."},
    {"name": "CF_ACCESS_TEAM_DOMAIN", "field": "cf_access_team_domain", "secret": False,
     "category": "exposure",
     "why": "Where Access identities are verified (see CF_ACCESS_ENABLED)."},
    {"name": "CF_ACCESS_AUD", "field": "cf_access_aud", "secret": False, "category": "exposure",
     "why": "The Access application audience (see CF_ACCESS_ENABLED)."},
    {"name": "CF_ACCESS_ALLOWED_EMAILS", "field": "cf_access_allowed_emails", "secret": False,
     "category": "exposure",
     "why": "Which Access identities may reach the admin plane."},
    {"name": "ALLOW_INSECURE_ADMIN", "field": "allow_insecure_admin", "secret": False,
     "category": "exposure",
     "why": "Escape hatch that weakens the boot interlock; only the host operator may set it."},
    {"name": "MCP_ALLOWED_HOSTS", "field": "mcp_allowed_hosts", "secret": False,
     "category": "exposure",
     "why": "The base Host allowlist for /mcp. The console can add hosts "
            "(mcp_allowed_hosts_extra) but never remove these."},
    {"name": "BROKER_DB", "field": "broker_db", "secret": False, "category": "deployment",
     "why": "Where the database lives; it has to be known before the database can be read."},
    {"name": "PLUGIN_URL_<SERVICE>, PLUGIN_TOKEN_<SERVICE>", "field": None, "secret": True,
     "category": "bootstrap",
     "why": "Where each plugin service answers and the token both ends share; generated "
            "per service so no container sees another's."},
    {"name": "PLUGIN_SECRETS_KEY_<SERVICE>, SIDECAR_TOKEN", "field": None, "secret": True,
     "category": "bootstrap",
     "why": "Held by the plugin containers only; the broker never receives them."},
    {"name": "SITE_DOMAIN, BROKER_PORT, TZ, DEVICE_NAME, GITHUB_APP_KEY_DIR", "field": None,
     "secret": False, "category": "compose",
     "why": "Read by Docker Compose, the edge or other containers, not by the broker."},
)


def _env_hosts() -> list[str]:
    raw = get_settings().mcp_allowed_hosts or ""
    return [h.strip() for h in raw.split(",") if h.strip()]


def _base(name: str) -> Any:
    if name == "mcp_allowed_hosts_extra":
        return ()
    return getattr(get_settings(), name)


def _stored() -> dict[str, Any]:
    """Valid console overrides. Invalid rows are skipped (the env default wins)."""
    with db.connect() as conn:
        rows = conn.execute("SELECT key, value FROM app_config WHERE key LIKE ?",
                            (PREFIX + "%",)).fetchall()
    out: dict[str, Any] = {}
    for r in rows:
        spec = SPECS.get(r["key"][len(PREFIX):])
        if spec is None:
            continue
        try:
            out[spec.name] = spec.validate(json.loads(r["value"]))
        except (ValueError, TypeError):
            continue
    return out


def runtime_settings() -> RuntimeSettings:
    """The effective operator settings for this request."""
    stored = _stored()
    values = {name: stored.get(name, _base(name)) for name in SPECS}
    hosts = _env_hosts()
    hosts += [h for h in values["mcp_allowed_hosts_extra"] if h not in hosts]
    return RuntimeSettings(**values, mcp_allowed_hosts=tuple(hosts))


def _env_set(name: str) -> bool:
    return any(k.upper() == name.upper() for k in os.environ)


def describe() -> dict:
    """What the console's Settings view shows: each editable setting with its
    value, env default and bounds, then the env-only keys and why. Secrets
    are reported as set/unset, never as values."""
    stored = _stored()
    eff = runtime_settings()
    items = []
    for name, spec in SPECS.items():
        default = _base(name)
        source = "console" if name in stored else ("env" if _env_set(name) else "default")
        item = {"name": name, "type": spec.kind, "unit": spec.unit, "help": spec.help,
                "value": getattr(eff, name), "default": default, "source": source}
        if spec.kind == "hosts":
            item["value"], item["default"] = list(item["value"]), list(default)
            item["base"] = _env_hosts()
            item["effective"] = list(eff.mcp_allowed_hosts)
        else:
            item["min"], item["max"] = spec.lo, spec.hi
        items.append(item)
    s = get_settings()
    env_only = []
    for e in ENV_ONLY:
        row = {"name": e["name"], "category": e["category"], "why": e["why"]}
        if e["field"] is not None:
            current = getattr(s, e["field"])
            if e["secret"]:
                row["set"] = bool(current)
            else:
                row["value"] = current
        env_only.append(row)
    return {"settings": items, "env_only": env_only}


def _encode(spec: Spec, value: Any) -> str:
    v = spec.validate(value)
    return json.dumps(list(v) if isinstance(v, tuple) else v)


def update(ctx, changes: dict) -> dict:
    """Apply a console edit: {name: value | None}. None resets to the env
    default. Everything is validated before anything is written, so a bad
    field leaves every setting untouched; the writes share one transaction."""
    if not isinstance(changes, dict) or not changes:
        raise PolicyError(400, "settings must be a non-empty object", "bad_request")
    env_only = {e["field"] for e in ENV_ONLY if e["field"]}
    writes: dict[str, str | None] = {}
    for name, value in changes.items():
        if name in env_only:
            raise PolicyError(400, f"{name} is env-only and cannot be changed from the console",
                              "env_only")
        spec = SPECS.get(name)
        if spec is None:
            raise PolicyError(400, f"unknown setting {name!r}", "unknown_setting")
        try:
            writes[name] = None if value is None else _encode(spec, value)
        except ValueError as exc:
            raise PolicyError(400, str(exc), "invalid_setting") from exc
    with db.connect() as conn:
        for name, encoded in writes.items():
            if encoded is None:
                conn.execute("DELETE FROM app_config WHERE key = ?", (PREFIX + name,))
            else:
                conn.execute("INSERT INTO app_config (key, value) VALUES (?, ?) ON CONFLICT(key)"
                             " DO UPDATE SET value = excluded.value", (PREFIX + name, encoded))
    # Runtime settings are never secrets (those go through crypto.py), so the
    # new values are safe to record alongside the names.
    audit(ctx.username, "settings.update", "",
          {"changed": sorted(writes),
           "values": {n: json.loads(v) if v is not None else None for n, v in writes.items()}},
          actor_principal=ctx.principal_id, actor_via=ctx.via)
    return describe()
