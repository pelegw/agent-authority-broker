"""Plugin config against the manifest's `config_schema`, and the plugins row.

Non-secret fields are validated here and stored in `plugins.config`.
Secret fields are validated for type and then handed back to the caller to
relay to the plugin's `/configure`; they are never written to broker.db,
never audited, never logged and never returned (the admin view shows only
which secret fields exist).

Shared fields (`shared: true`, e.g. the one Google OAuth client behind
gmail, gcal and gdrive) belong to the connection's shared slot rather than
to one plugin: the console renders them once per slot (one "Google
account" form), a non-secret value is kept identical on every plugin of the
slot (`shared_siblings` + plugins_admin.patch_config), and a secret one is
relayed once and stored once, in that slot, by the plugin runtime.
"""

from __future__ import annotations

import json
import time
from typing import Any

from .. import db
from ..errors import PolicyError
from .manifest import ConfigField, Manifest


def _type_ok(f: ConfigField, value: Any) -> bool:
    if f.type in ("string", "text"):
        return isinstance(value, str) and len(value) <= (65536 if f.type == "text" else 4096)
    if f.type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if f.type == "boolean":
        return isinstance(value, bool)
    return value in (f.values or [])


def split_patch(manifest: Manifest, patch: Any) -> tuple[dict, dict]:
    """Validate a config patch; returns (non_secret_updates, secrets).

    A non-secret value of None resets the field to its default; a secret of
    None or "" asks the plugin to forget it.
    """
    if not isinstance(patch, dict):
        raise PolicyError(400, "config must be an object", "invalid_config")
    fields = {f.name: f for f in manifest.config_schema}
    config, secrets = {}, {}
    for name, value in patch.items():
        f = fields.get(name)
        if f is None:
            raise PolicyError(400, f"unknown config field {name!r}", "invalid_config")
        if f.secret:
            if value is not None and not isinstance(value, str):
                raise PolicyError(400, f"secret field {name!r} must be a string",
                                  "invalid_config")
            secrets[name] = value
        elif value is None:
            config[name] = None
        elif not _type_ok(f, value):
            raise PolicyError(400, f"config field {name!r} must be {f.type}"
                              + (f" (one of {f.values})" if f.values else ""),
                              "invalid_config")
        else:
            config[name] = value
    return config, secrets


def merge(stored: dict, updates: dict) -> dict:
    out = dict(stored)
    for k, v in updates.items():
        if v is None:
            out.pop(k, None)
        else:
            out[k] = v
    return out


def effective_config(manifest: Manifest, stored: dict) -> dict:
    """Defaults overlaid with stored non-secret values (secrets never here)."""
    out = {f.name: f.default for f in manifest.config_schema
           if not f.secret and f.default is not None}
    out.update({k: v for k, v in stored.items()
                if any(f.name == k and not f.secret for f in manifest.config_schema)})
    return out


def missing_required(manifest: Manifest, config: dict) -> list[str]:
    """Required non-secret fields with no value. Required secrets cannot be
    checked here (the broker never holds them); the plugin reports those."""
    return [f.name for f in manifest.config_schema
            if f.required and not f.secret and config.get(f.name) in (None, "")]


def schema_view(manifest: Manifest) -> list[dict]:
    return [{"name": f.name, "type": f.type, "secret": f.secret, "required": f.required,
             "default": f.default, "help": f.help, "values": f.values, "shared": f.shared}
            for f in manifest.config_schema]


def shared_names(manifest: Manifest) -> frozenset[str]:
    """Config fields that belong to the manifest's shared connection slot."""
    return frozenset(f.name for f in manifest.config_schema if f.shared)


def shared_siblings(manifest: Manifest, manifests: dict[str, Manifest]) -> list[str]:
    """Other plugin ids whose manifests use the same shared connection slot."""
    slot = manifest.connection.shared
    if not slot:
        return []
    return sorted(pid for pid, m in manifests.items()
                  if pid != manifest.id and m.connection.shared == slot)


# ---- the plugins row -------------------------------------------------------------

def store_config(plugin_id: str, config: dict) -> None:
    with db.connect() as conn:
        conn.execute("UPDATE plugins SET config = ?, updated_at = ? WHERE id = ?",
                     (json.dumps(config, sort_keys=True), int(time.time()), plugin_id))


def set_enabled(plugin_id: str, enabled: bool) -> None:
    with db.connect() as conn:
        conn.execute("UPDATE plugins SET enabled = ?, updated_at = ? WHERE id = ?",
                     (1 if enabled else 0, int(time.time()), plugin_id))


def set_health(plugin_id: str, health: dict, connected: bool | None) -> None:
    """Store a health answer. `connected=None` keeps the last known value:
    an unreachable plugin is unhealthy, but only a real answer (or a
    disconnect) changes whether it is connected."""
    with db.connect() as conn:
        if connected is None:
            conn.execute("UPDATE plugins SET last_health = ?, updated_at = ? WHERE id = ?",
                         (json.dumps(health, sort_keys=True), int(time.time()), plugin_id))
        else:
            conn.execute("UPDATE plugins SET last_health = ?, connected = ?, updated_at = ?"
                         " WHERE id = ?", (json.dumps(health, sort_keys=True),
                                           1 if connected else 0, int(time.time()), plugin_id))
