"""Owner-approved manifests for plugins that are not vendored in the broker.

The pin is the authority property "a container can never widen its own
lattice": the broker registers a plugin only when the manifest it offers
matches a copy the owner approved, and from then on it uses that copy, never
the plugin's. In-tree plugins are pinned by the vendored file under
`broker/broker/targets/<id>/manifest.yaml`. An external plugin (installed from
its own repository) has no file in the tree, so its approved copy lives here,
in the `plugin_pins` table, written only by an owner action (`plugin.pin`,
audited in services/plugins_admin.py).

This is a pure database module on purpose. The registry imports it, and
agents reach the registry, so it must never import `deps` or `identity`
(tests/test_no_admin_from_agent_paths.py walks that graph). Who may pin is
decided by the admin router and service; this module only stores, validates
and describes.

`summary()` and `diff()` build the review card the console shows before the
owner pins: what the plugin can do (actions, side effects, modes), what a
grant can narrow, the constraints, and the config it will ask for (which
fields are secret); on an upgrade, what changed against the current pin.
"""

from __future__ import annotations

import time
from typing import Any

from .. import db
from .manifest import ID_RE, Manifest, ManifestError, load_manifest_text

# The provenance fields a pin carries besides the manifest. `commit` is
# stored as `commit_sha` (COMMIT is an SQL keyword).
_COLUMNS = "plugin_id, version, source, ref, commit_sha, pinned_at, pinned_by"


def _valid_id(plugin_id: Any) -> bool:
    return isinstance(plugin_id, str) and ID_RE.match(plugin_id) is not None


def _record(row) -> dict:
    return {"plugin_id": row["plugin_id"], "version": row["version"], "source": row["source"],
            "ref": row["ref"], "commit": row["commit_sha"], "pinned_at": row["pinned_at"],
            "pinned_by": row["pinned_by"]}


# ---- storage --------------------------------------------------------------------

def get(plugin_id: str) -> Manifest | None:
    """The pinned manifest for `plugin_id`, or None when nothing is pinned.

    Raises ManifestError when the stored copy no longer validates (a broker
    upgrade made the schema stricter): the registry then refuses the plugin,
    exactly as it does for an invalid vendored file."""
    if not _valid_id(plugin_id):
        return None                       # never query with junk
    with db.connect() as conn:
        row = conn.execute("SELECT manifest_yaml FROM plugin_pins WHERE plugin_id = ?",
                           (plugin_id,)).fetchone()
    if row is None:
        return None
    m = load_manifest_text(row["manifest_yaml"])
    if m.id != plugin_id:
        # Only set() writes rows and it checks this; a mismatch means the row
        # was edited by hand. Refuse rather than serve one id under another.
        raise ManifestError(f"pinned manifest for {plugin_id!r} declares id {m.id!r}")
    return m


def record(plugin_id: str) -> dict | None:
    """The pin's provenance (version, source, ref, commit, who, when), no manifest."""
    if not _valid_id(plugin_id):
        return None
    with db.connect() as conn:
        row = conn.execute(f"SELECT {_COLUMNS} FROM plugin_pins WHERE plugin_id = ?",
                           (plugin_id,)).fetchone()
    return _record(row) if row else None


# `all` and `set` shadow the builtins inside this module only (callers write
# pins.all(), pins.set()); nothing below uses the builtins.
def all() -> list[dict]:
    """Every pin's provenance, sorted by plugin id."""
    with db.connect() as conn:
        rows = conn.execute(f"SELECT {_COLUMNS} FROM plugin_pins ORDER BY plugin_id").fetchall()
    return [_record(r) for r in rows]


def set(plugin_id: str, manifest_text: str, source: str = "", ref: str = "",
        commit: str = "", by: str = "") -> Manifest:
    """Validate `manifest_text` and store it as the pin for `plugin_id`,
    replacing any earlier pin. Returns the validated manifest.

    Raises ManifestError when the text is not a valid manifest or declares
    another id, and ValueError when `by` (the owner's username) is missing:
    a pin nobody can be held to is not written."""
    if not _valid_id(plugin_id):
        raise ManifestError(f"plugin id {plugin_id!r} must match {ID_RE.pattern}")
    if not isinstance(by, str) or not by.strip():
        raise ValueError("a pin must record the owner who approved it")
    m = load_manifest_text(manifest_text)
    if m.id != plugin_id:
        raise ManifestError(f"manifest declares id {m.id!r}, not {plugin_id!r}")
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO plugin_pins (plugin_id, version, manifest_yaml, source, ref,"
            " commit_sha, pinned_at, pinned_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(plugin_id) DO UPDATE SET version = excluded.version,"
            " manifest_yaml = excluded.manifest_yaml, source = excluded.source,"
            " ref = excluded.ref, commit_sha = excluded.commit_sha,"
            " pinned_at = excluded.pinned_at, pinned_by = excluded.pinned_by",
            (plugin_id, m.version, manifest_text, source or "", ref or "", commit or "",
             int(time.time()), by))
    return m


def delete(plugin_id: str) -> bool:
    """Remove the pin. Returns whether there was one."""
    if not _valid_id(plugin_id):
        return False
    with db.connect() as conn:
        cur = conn.execute("DELETE FROM plugin_pins WHERE plugin_id = ?", (plugin_id,))
    return cur.rowcount > 0


# ---- the review card -----------------------------------------------------------------

def _action(a) -> dict:
    return {"name": a.name, "side_effect": a.side_effect, "modes": list(a.effective_modes),
            "resource": a.resource, "schedulable": a.schedulable, "doc": a.doc}


def _narrowing(n) -> dict:
    return {"dimension": n.dimension, "form": n.form, "resource": n.resource,
            "applies_to": list(n.applies_to), "enforcement": n.enforcement,
            "derived": n.derived_from is not None, "values": n.values}


def _constraint(c) -> dict:
    return {"name": c.name, "form": c.form, "applies_to": list(c.applies_to),
            "enforcement": c.enforcement, "values": c.values, "default": c.default}


def _config(f) -> dict:
    return {"name": f.name, "type": f.type, "secret": f.secret, "shared": f.shared,
            "required": f.required}


def summary(manifest: Manifest) -> dict:
    """What the owner approves by pinning `manifest`, as plain data."""
    effects = {e: sorted(manifest.actions_by_effect(e)) for e in ("read", "write", "destructive")}
    return {
        "id": manifest.id, "version": manifest.version,
        "display_name": manifest.display_name, "description": manifest.description,
        "connection": {"kind": manifest.connection.kind,
                       "enforcement": manifest.connection.enforcement,
                       "shared": manifest.connection.shared},
        "actions": [_action(a) for a in manifest.actions],
        "side_effects": effects,
        "resources": sorted(manifest.resources),
        "narrowings": [_narrowing(n) for n in manifest.narrowings],
        "constraints": [_constraint(c) for c in manifest.constraints],
        "config": [_config(f) for f in manifest.config_schema],
        "secret_config": [f.name for f in manifest.config_schema if f.secret],
    }


def _changes(old: dict[str, dict], new: dict[str, dict]) -> dict:
    """added / removed / changed names between two {name: spec} maps."""
    return {"added": sorted(k for k in new if k not in old),
            "removed": sorted(k for k in old if k not in new),
            "changed": sorted(k for k in new if k in old and new[k] != old[k])}


def diff(old: Manifest | None, new: Manifest) -> dict:
    """What pinning `new` changes against the current pin `old` (None for a
    first pin: everything is added). `actions.changed` carries the side
    effect and mode moves, the part of an upgrade most worth reading."""
    def by_name(m: Manifest | None, items: str, key: str, view) -> dict[str, dict]:
        return {getattr(x, key): view(x) for x in getattr(m, items)} if m else {}

    old_actions = by_name(old, "actions", "name", _action)
    new_actions = by_name(new, "actions", "name", _action)
    actions = _changes(old_actions, new_actions)
    actions["changed"] = [
        {"name": name,
         "side_effect": {"from": old_actions[name]["side_effect"],
                         "to": new_actions[name]["side_effect"]},
         "modes": {"from": old_actions[name]["modes"], "to": new_actions[name]["modes"]}}
        for name in actions["changed"]]
    narrowings = _changes(by_name(old, "narrowings", "dimension", _narrowing),
                          by_name(new, "narrowings", "dimension", _narrowing))
    constraints = _changes(by_name(old, "constraints", "name", _constraint),
                           by_name(new, "constraints", "name", _constraint))
    old_config = by_name(old, "config_schema", "name", _config)
    new_config = by_name(new, "config_schema", "name", _config)
    config = _changes(old_config, new_config)
    # A field that is new, or newly secret: the owner will be asked to enter it.
    config["secret_added"] = sorted(
        k for k, f in new_config.items()
        if f["secret"] and not old_config.get(k, {}).get("secret", False))
    connection = None if old is None else {
        "from": old.connection.model_dump(), "to": new.connection.model_dump()}
    if connection and connection["from"] == connection["to"]:
        connection = None
    sections = (actions, narrowings, constraints, config)
    changed = (old is None or old.version != new.version or connection is not None
               or any(s[k] for s in sections for k in ("added", "removed", "changed")))
    return {"from_version": old.version if old else None, "to_version": new.version,
            "changed": changed, "actions": actions, "narrowings": narrowings,
            "constraints": constraints, "config": config, "connection": connection}
