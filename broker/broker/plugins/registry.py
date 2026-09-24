"""The plugin registry: which plugins exist, how to reach them, and the lattice.

Discovery: every plugin service configured in env (PLUGIN_URL_<SERVICE> +
PLUGIN_TOKEN_<SERVICE>, see config.plugin_services) is asked for
`GET /manifests`. Each returned manifest is **pinned** against the vendored
copy at `broker/broker/targets/<id>/manifest.yaml`: id and version must
match, and the vendored copy (not the plugin's) is what the broker uses from
then on, so a plugin cannot widen its declared lattice at runtime. A plugin
that fails the pin is refused and audited. Tests register the in-process
`echo` plugin through `register_in_process`, which applies the same pin.

State: enabled/config/connected live in the `plugins` table (a row is
created, disabled, on first sight). The registry holds only the process-local
things (adapters, validated manifests, an ancestry cache) and re-reads the
table on every call, so a toggle in the console takes effect on the next
request. The broker runs one worker, so a module-level singleton suffices.

`lattice()` is the ONE place a `Lattice` is built for live evaluation: forms
from the validated manifests plus `ancestors(kind, id)` backed by the
owning plugin's parent lookup, cached for `ancestors_cache_seconds`.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import db
from ..audit import audit
from ..authority.grant import Lattice
from ..config import get_settings, plugin_services
from .adapter import Adapter, AdapterError, ClientFactory, InProcessAdapter, RemoteAdapter, request
from .manifest import ID_RE, Manifest, ManifestError, load_manifest

log = logging.getLogger(__name__)

TARGETS_DIR = Path(__file__).resolve().parents[1] / "targets"
# A plugin service that was unreachable at boot is retried at most this often.
_REDISCOVER_SECONDS = 30


@dataclass(frozen=True)
class Entry:
    manifest: Manifest       # the vendored (pinned) manifest
    adapter: Adapter
    service: str


class Registry:
    def __init__(self, vendored_dirs: tuple[Path, ...] = (TARGETS_DIR,)):
        self._vendored_dirs = tuple(Path(d) for d in vendored_dirs)
        self._entries: dict[str, Entry] = {}
        self.refused: dict[str, str] = {}
        self._pending: dict[str, tuple[str, str]] = {}
        self._next_discovery = 0.0
        self._factory: ClientFactory | None = None
        self._anc_cache: dict[tuple[str, str], tuple[float, tuple[str, ...]]] = {}
        self._lock = threading.RLock()

    # ---- registration ------------------------------------------------------

    def vendored(self, plugin_id: str) -> Manifest | None:
        """The pinned manifest for `plugin_id`, or None if none is vendored."""
        if not isinstance(plugin_id, str) or not ID_RE.match(plugin_id):
            return None                       # never build a path from junk
        for d in self._vendored_dirs:
            path = d / plugin_id / "manifest.yaml"
            if path.is_file():
                return load_manifest(path)
        return None

    def _pin(self, offered: Any, vendored: Manifest | None, service: str) -> Manifest | None:
        """Validate an offered manifest against its vendored copy; audit refusals."""
        pid = offered.get("id") if isinstance(offered, dict) else None
        label = pid if isinstance(pid, str) else "(unknown)"
        reason = None
        if vendored is None:
            reason = "no vendored manifest for this plugin id"
        elif pid != vendored.id or not isinstance(offered, dict) or \
                offered.get("version") != vendored.version:
            reason = (f"manifest mismatch: plugin offers {pid}@{offered.get('version')}, "
                      f"vendored is {vendored.id}@{vendored.version}"
                      if isinstance(offered, dict) else "manifest is not an object")
        elif pid in self._entries and self._entries[pid].service != service:
            reason = f"plugin id already served by {self._entries[pid].service!r}"
        if reason:
            self.refused[label] = reason
            log.warning("refusing plugin %s from %s: %s", label, service, reason)
            audit("system", "plugin.refused", label, {"service": service, "reason": reason},
                  result="denied")
            return None
        self.refused.pop(label, None)
        return vendored

    def register(self, adapter: Adapter, offered: dict | Manifest,
                 vendored: Manifest | None = None) -> bool:
        """Register an adapter whose plugin offered `offered`. Returns False
        (and audits) when the pin fails."""
        if isinstance(offered, Manifest):
            offered = offered.model_dump(mode="json")
        with self._lock:
            pid = offered.get("id") if isinstance(offered, dict) else None
            if vendored is None and isinstance(pid, str):
                vendored = self.vendored(pid)
            pinned = self._pin(offered, vendored, adapter.service)
            if pinned is None:
                return False
            adapter.manifest = pinned
            self._entries[pinned.id] = Entry(pinned, adapter, adapter.service)
            ensure_row(pinned.id)
            return True

    def register_in_process(self, impl: Any, vendored: Manifest,
                            service: str = "inprocess") -> bool:
        """Register a plugin-side adapter object living in this process."""
        offered = getattr(impl, "manifest", None)
        return self.register(InProcessAdapter(impl, vendored, service), offered, vendored)

    def unregister(self, plugin_id: str) -> None:
        with self._lock:
            self._entries.pop(plugin_id, None)

    # ---- discovery -------------------------------------------------------------

    def discover(self, services: dict[str, tuple[str, str]] | None = None,
                 client_factory: ClientFactory | None = None) -> None:
        """Fetch and pin every configured service's manifests."""
        with self._lock:
            if client_factory is not None:
                self._factory = client_factory
            todo = plugin_services() if services is None else services
            self._pending = {}
            for service, (url, token) in todo.items():
                self._discover_one(service, url, token)
            self._next_discovery = time.monotonic() + _REDISCOVER_SECONDS

    def _discover_one(self, service: str, url: str, token: str) -> None:
        timeout = get_settings().plugin_timeout_seconds
        try:
            body = request(url, token, "GET", "/manifests", timeout=timeout,
                           factory=self._factory)
        except AdapterError as exc:
            # Down at boot is normal (containers start in any order): retry later.
            self._pending[service] = (url, token)
            log.warning("plugin service %s not reachable yet: %s", service, exc.message)
            return
        offered = body.get("manifests")
        if not isinstance(offered, list):
            audit("system", "plugin.refused", service,
                  {"service": service, "reason": "malformed /manifests"}, result="denied")
            return
        for m in offered:
            pid = m.get("id") if isinstance(m, dict) else None
            try:
                vendored = self.vendored(pid) if isinstance(pid, str) else None
            except ManifestError as exc:
                log.error("vendored manifest for %s is invalid: %s", pid, exc)
                vendored = None
            if vendored is None:
                self._pin(m, None, service)
                continue
            adapter = RemoteAdapter(service, url, token, vendored, timeout, self._factory)
            self.register(adapter, m, vendored)

    def _maybe_rediscover(self) -> None:
        if self._pending and time.monotonic() >= self._next_discovery:
            self.discover(dict(self._pending))

    # ---- reads (each re-reads the plugins table) -----------------------------

    def entries(self) -> dict[str, Entry]:
        self._maybe_rediscover()
        return dict(self._entries)

    def manifests(self) -> dict[str, Manifest]:
        """Every registered plugin's pinned manifest, enabled or not."""
        return {pid: e.manifest for pid, e in self.entries().items()}

    def adapter(self, target: str) -> Adapter | None:
        e = self.entries().get(target)
        return e.adapter if e else None

    def service_of(self, target: str) -> str | None:
        e = self.entries().get(target)
        return e.service if e else None

    def plugin_rows(self) -> dict[str, dict]:
        return plugin_rows()

    def enabled_plugins(self) -> list[str]:
        rows = plugin_rows()
        return sorted(pid for pid in self.entries() if rows.get(pid, {}).get("enabled") == 1)

    def enabled_manifests(self) -> dict[str, Manifest]:
        entries = self.entries()
        return {pid: entries[pid].manifest for pid in self.enabled_plugins()}

    def plugin_states(self) -> list[tuple[Manifest, bool, bool]]:
        """(manifest, enabled, connected) for the ceiling. `connected` is the
        last successful health answer, stored by plugins/settings.py."""
        rows = plugin_rows()
        out = []
        for pid, e in sorted(self.entries().items()):
            row = rows.get(pid, {})
            out.append((e.manifest, row.get("enabled") == 1, row.get("connected") == 1))
        return out

    def is_enabled(self, target: str) -> bool:
        return target in self.enabled_plugins()

    def last_health(self, target: str) -> dict:
        return plugin_rows().get(target, {}).get("last_health", {})

    # ---- the lattice ------------------------------------------------------------

    def lattice(self) -> Lattice:
        return Lattice.from_manifests(self.manifests(), self.ancestors)

    def ancestors(self, kind: str, resource_id: str) -> tuple[str, ...]:
        """Parent chain of a resource, nearest first, for subtree narrowing.

        The lattice asks by resource kind only. When exactly one registered
        plugin has a subtree narrowing over `kind` it answers; when none or
        several do, the answer is "no ancestry". That is always the narrow
        side of the algebra (only exact ids match), so an ambiguous kind name
        can never let one plugin's folder tree widen another's. Plugin
        failures are not cached and also answer "no ancestry" (fail closed).
        """
        owners = [e for e in self._entries.values()
                  if any(n.form == "subtree" and n.resource == kind
                         for n in e.manifest.narrowings)]
        if len(owners) != 1:
            return ()
        now = time.monotonic()
        key = (kind, resource_id)
        with self._lock:
            hit = self._anc_cache.get(key)
            if hit and hit[0] > now:
                return hit[1]
        try:
            chain = tuple(owners[0].adapter.ancestors(kind, resource_id))
        except AdapterError:
            return ()
        with self._lock:
            self._anc_cache[key] = (now + get_settings().ancestors_cache_seconds, chain)
        return chain

    def clear_cache(self) -> None:
        with self._lock:
            self._anc_cache.clear()


# ---- the plugins table ----------------------------------------------------------

def ensure_row(plugin_id: str) -> None:
    """Create the plugin's row, disabled, if it does not exist yet."""
    with db.connect() as conn:
        conn.execute("INSERT OR IGNORE INTO plugins (id, enabled, config, connected,"
                     " last_health, updated_at) VALUES (?, 0, '{}', 0, '{}', ?)",
                     (plugin_id, int(time.time())))


def plugin_rows() -> dict[str, dict]:
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM plugins").fetchall()
    out = {}
    for r in rows:
        d = dict(r)
        for col in ("config", "last_health"):
            try:
                d[col] = json.loads(d[col] or "{}")
            except ValueError:
                d[col] = {}
        out[d["id"]] = d
    return out


# ---- the singleton -----------------------------------------------------------------

_registry: Registry | None = None
_singleton_lock = threading.Lock()


def get_registry() -> Registry:
    global _registry
    with _singleton_lock:
        if _registry is None:
            _registry = Registry()
        return _registry


def reset_registry(registry: Registry | None = None) -> Registry:
    """Replace the singleton (tests, and a fresh boot)."""
    global _registry
    with _singleton_lock:
        _registry = registry or Registry()
        return _registry


def init_registry() -> Registry:
    """Called from the app lifespan: discover every configured service."""
    reg = get_registry()
    reg.discover()
    return reg
