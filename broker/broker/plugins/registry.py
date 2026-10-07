"""The plugin registry: which plugins exist, how to reach them, and the lattice.

Discovery: every known plugin service is asked for `GET /manifests`. A
service is known from two sources, merged by `services()`:

  * env: PLUGIN_URL_<SERVICE> + PLUGIN_TOKEN_<SERVICE> (config.plugin_services),
    fixed when the container was created;
  * dynamic: the installer's `GET /services` (services/plugin_install.py
    reconcile_services -> set_dynamic_services), so a plugin installed while
    the broker runs is reached without recreating the broker.

The merge rule. Until the installer's first successful answer since boot
(and always when no installer is configured), env stands alone, as without
an installer. From that answer on, the installer is the authority for every
external service:

  * a reserved name (RESERVED_SERVICES: the in-tree services) always comes
    from env and is never evicted; the installer can never list one (refused
    here as well as in the installer);
  * a service the installer lists uses the installer's URL and token (it
    reads them from .env at each request, so they are fresher than an env
    snapshot: a purge and reinstall mints a new token);
  * any other env service is evicted: the broker's env still names a plugin
    removed since this container was created (a `docker restart` keeps the
    old env) until the next deploy recreates it.

The installer's URL is only ever `http://plugin-<service>:8090`, so it can
never redirect an in-tree service or point the broker anywhere else.
Eviction drops the service's plugin ids at once (agents get 404) and its
pending retry (no warning every 30 s); its plugins rows stay.

Each returned manifest is **pinned** against an
owner-approved copy: the vendored file at `broker/broker/targets/<id>/
manifest.yaml` for an in-tree plugin, else the owner's pin in the
`plugin_pins` table (plugins/pins.py) for an external one. In-tree always
wins, so no database row can shadow a plugin shipped with the broker. Id and
version must match, and the approved copy (not the plugin's) is what the
broker uses from then on, so a plugin cannot widen its declared lattice at
runtime. A plugin that fails the pin is refused and audited, and its offer is
kept in `offered` so the console can show it for review; pinning it (an owner
action, services/plugins_admin.py) then calls `repin()`, which registers it
without waiting for a rediscovery. Tests register the in-process `echo`
plugin through `register_in_process`, which applies the same pin.

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
import re
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .. import db
from ..audit import audit
from ..authority.grant import Lattice
from ..config import plugin_services
from ..logging_setup import kv
from ..runtime_settings import runtime_settings
from . import pins
from .adapter import Adapter, AdapterError, ClientFactory, InProcessAdapter, RemoteAdapter, request
from .manifest import ID_RE, Manifest, ManifestError, load_manifest

log = logging.getLogger(__name__)

TARGETS_DIR = Path(__file__).resolve().parents[1] / "targets"
# A plugin service that was unreachable at boot is retried at most this often.
_REDISCOVER_SECONDS = 30

# Dynamic services (the installer's list) must have exactly this shape.
# The names the stack's own services use: installer/aab_installer/descriptor.py
# RESERVED_SERVICES, kept equal by tests/test_registry.py. The installer
# never lists one; the registry refuses one anyway.
RESERVED_SERVICES = frozenset({
    "broker", "edge", "caddy", "installer", "sidecar", "internal", "default",
    "whatsapp", "wa", "github", "google", "plugin", "plugins",
})
SERVICE_RE = re.compile(r"^[a-z][a-z0-9]{1,31}$")
PLUGIN_PORT = 8090             # what the plugin runtime serves on (the overlay's URL)
_TOKEN_RE = re.compile(r"^[A-Za-z0-9._~+/=-]{16,256}$")
MAX_DYNAMIC = 64


def dynamic_url(service: str) -> str:
    """The one URL a dynamic service may have (the installer's overlay)."""
    return f"http://plugin-{service}:{PLUGIN_PORT}"


def check_dynamic(mapping: Any) -> dict[str, tuple[str, str]]:
    """The installer's services, validated strictly: {service: (url, token)}.
    Raises ValueError on anything else, naming the service at most (never
    a URL or a token), so the caller keeps the previous set."""
    if not isinstance(mapping, dict):
        raise ValueError("the services are not a mapping")
    if len(mapping) > MAX_DYNAMIC:
        raise ValueError(f"more than {MAX_DYNAMIC} services")
    out: dict[str, tuple[str, str]] = {}
    for service, value in mapping.items():
        if not isinstance(service, str) or not SERVICE_RE.match(service):
            raise ValueError("a service name is malformed")
        if service in RESERVED_SERVICES:
            raise ValueError(f"service {service!r} is a name the stack itself uses")
        if not isinstance(value, tuple) or len(value) != 2:
            raise ValueError(f"service {service!r}: malformed entry")
        url, token = value
        if url != dynamic_url(service):
            raise ValueError(f"service {service!r}: the url is not {dynamic_url(service)}")
        if not isinstance(token, str) or not _TOKEN_RE.match(token):
            raise ValueError(f"service {service!r}: malformed token")
        out[service] = (url, token)
    return dict(sorted(out.items()))


@dataclass(frozen=True)
class DynamicChange:
    """What set_dynamic_services changed. `fresh` are the services to
    discover now (new, or reached at another URL or with another token);
    `removed` were evicted. Never logged whole: `fresh` holds tokens."""
    fresh: dict[str, tuple[str, str]] = field(repr=False)
    removed: tuple[str, ...]


def live_plugin_timeout() -> float:
    """The console's plugin_timeout_seconds, read at call time."""
    return runtime_settings().plugin_timeout_seconds


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
        # Every refused offer, for the owner to review: {plugin id: {"manifest":
        # what the service offered (raw, unvalidated), "service", "reason"}}.
        self.offered: dict[str, dict] = {}
        # How to build an adapter for a refused offer once it is pinned
        # (repin). Private: a remote one closes over the service token.
        self._retry: dict[str, Callable[[Manifest], Adapter]] = {}
        # The raw manifest each registered plugin offered, so withdraw() can
        # put it back up for review.
        self._raw_offers: dict[str, Any] = {}
        self._pending: dict[str, tuple[str, str]] = {}
        # The installer's services (memory only: tokens are never written
        # anywhere by the broker), and whether it has answered since boot.
        self._dynamic: dict[str, tuple[str, str]] = {}
        self._synced = False
        self._next_discovery = 0.0
        self._factory: ClientFactory | None = None
        self._anc_cache: dict[tuple[str, str], tuple[float, tuple[str, ...]]] = {}
        self._lock = threading.RLock()

    # ---- registration ------------------------------------------------------

    def vendored(self, plugin_id: str) -> Manifest | None:
        """The approved manifest for `plugin_id`: the in-tree file, else the
        owner's database pin, else None. Raises ManifestError when the copy
        found is invalid."""
        if not isinstance(plugin_id, str) or not ID_RE.match(plugin_id):
            return None                       # never build a path from junk
        in_tree = self.in_tree(plugin_id)
        if in_tree is not None:
            return in_tree
        return pins.get(plugin_id)

    def in_tree(self, plugin_id: str) -> Manifest | None:
        """The vendored file's manifest, or None. Plugins shipped with the
        broker are pinned by the tree alone; a database pin never applies."""
        if not isinstance(plugin_id, str) or not ID_RE.match(plugin_id):
            return None
        for d in self._vendored_dirs:
            path = d / plugin_id / "manifest.yaml"
            if path.is_file():
                return load_manifest(path)
        return None

    def _safe_vendored(self, plugin_id: str) -> Manifest | None:
        try:
            return self.vendored(plugin_id)
        except ManifestError as exc:
            log.error("vendored manifest is invalid %s", kv(plugin=plugin_id, error=str(exc)))
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
            self.offered[label] = {"manifest": offered, "service": service, "reason": reason}
            log.warning("plugin refused %s", kv(plugin=label, service=service, reason=reason))
            audit("system", "plugin.refused", label, {"service": service, "reason": reason},
                  result="denied")
            return None
        self.refused.pop(label, None)
        self.offered.pop(label, None)
        self._retry.pop(label, None)
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
                if isinstance(pid, str) and ID_RE.match(pid):
                    self._retry[pid] = lambda _m, a=adapter: a
                return False
            adapter.manifest = pinned
            known = pinned.id in self._entries
            self._entries[pinned.id] = Entry(pinned, adapter, adapter.service)
            self._raw_offers[pinned.id] = offered
            ensure_row(pinned.id)
            if not known:
                log.info("plugin registered %s", kv(plugin=pinned.id, service=adapter.service,
                                                    version=pinned.version))
            return True

    def register_in_process(self, impl: Any, vendored: Manifest,
                            service: str = "inprocess") -> bool:
        """Register a plugin-side adapter object living in this process."""
        offered = getattr(impl, "manifest", None)
        return self.register(InProcessAdapter(impl, vendored, service), offered, vendored)

    def unregister(self, plugin_id: str) -> None:
        with self._lock:
            self._entries.pop(plugin_id, None)

    def offers(self) -> dict[str, dict]:
        """A snapshot of the refused offers awaiting review."""
        with self._lock:
            return {pid: dict(o) for pid, o in self.offered.items()}

    def repin(self, plugin_id: str) -> bool:
        """Re-run the pin for a refused offer now (after the owner pinned
        it), instead of waiting for the next discovery. Returns whether the
        plugin is registered. Uses the manifest the service offered at
        discovery; whatever it offers, the approved copy is what is used."""
        with self._lock:
            offer = self.offered.get(plugin_id)
            make = self._retry.get(plugin_id)
            if offer is None or make is None:
                return False
            vendored = self._safe_vendored(plugin_id)
            if vendored is None:
                self._pin(offer["manifest"], None, offer["service"])    # still refused
                return False
            return self.register(make(vendored), offer["manifest"], vendored)

    def withdraw(self, plugin_id: str, reason: str) -> bool:
        """Stop serving a registered plugin whose approval was removed (an
        unpin), and put its offer back up for review. Returns whether it was
        registered. Agents get 404 for it from the next call on."""
        with self._lock:
            entry = self._entries.pop(plugin_id, None)
            if entry is None:
                return False
            offered = self._raw_offers.pop(plugin_id, None)
            if offered is None:
                offered = entry.manifest.model_dump(mode="json")
            self.refused[plugin_id] = reason
            self.offered[plugin_id] = {"manifest": offered, "service": entry.service,
                                       "reason": reason}
            self._retry[plugin_id] = lambda _m, a=entry.adapter: a
            log.warning("plugin withdrawn %s", kv(plugin=plugin_id, service=entry.service,
                                                  reason=reason))
            return True

    # ---- known services: env plus the installer's ---------------------------------

    def _merged(self, env: dict[str, tuple[str, str]]) -> dict[str, tuple[str, str]]:
        """The merge rule (module docstring): before the installer's first
        answer, env; after it, reserved names from env plus the installer's
        list, and nothing else."""
        if not self._synced:
            return dict(sorted(env.items()))
        out = {s: v for s, v in env.items() if s in RESERVED_SERVICES}
        out.update(self._dynamic)
        return dict(sorted(out.items()))

    def services(self) -> dict[str, tuple[str, str]]:
        """{service: (url, token)} for every known plugin service."""
        with self._lock:
            return self._merged(plugin_services())

    def dynamic_services(self) -> list[str]:
        """The names the installer lists now (no URL, no token)."""
        with self._lock:
            return sorted(self._dynamic)

    def set_dynamic_services(self, mapping: Any) -> DynamicChange:
        """Replace the installer's services with `mapping` ({service: (url,
        token)}), after a successful fetch. Validated whole first (ValueError,
        nothing changed). From then on the merge rule makes the installer
        the authority for every external service: one it does not list is
        evicted, env or not. The services to discover are returned, not
        discovered here, so the caller decides when the network calls
        happen."""
        new = check_dynamic(mapping)
        with self._lock:
            env = plugin_services()
            before = self._merged(env)
            self._dynamic = new
            self._synced = True
            after = self._merged(env)
            removed = tuple(s for s in before if s not in after)
            for service in removed:
                self._evict(service)
            fresh = {s: v for s, v in after.items() if before.get(s) != v}
            for service in sorted(set(new) & set(env)):
                if env[service] != new[service] and service in fresh:
                    # Which source won and which field differs; never a value.
                    differs = [n for n, a, b in zip(("url", "token"), env[service],
                                                    new[service]) if a != b]
                    log.debug("plugin service set by env and by the installer; the "
                              "installer's values win %s",
                              kv(service=service, used="installer", differs=differs))
            return DynamicChange(fresh, removed)

    def _evict(self, service: str) -> None:
        """Forget a service: its plugin ids stop being served at once, its
        offers and pending retry go. Plugins rows and pins are untouched."""
        gone = sorted(pid for pid, e in self._entries.items() if e.service == service)
        for pid in gone:
            self._entries.pop(pid, None)
            self._raw_offers.pop(pid, None)
        for pid in [p for p, o in self.offered.items() if o.get("service") == service]:
            self.offered.pop(pid, None)
            self.refused.pop(pid, None)
            self._retry.pop(pid, None)
        self._pending.pop(service, None)
        self._anc_cache.clear()          # a cached chain may have come from it
        log.info("plugin service removed %s", kv(service=service, plugins=gone))

    # ---- discovery -------------------------------------------------------------

    def discover(self, services: dict[str, tuple[str, str]] | None = None,
                 client_factory: ClientFactory | None = None) -> None:
        """Fetch and pin the manifests of `services` (default: every known
        service). Only those services' pending retries are reset."""
        with self._lock:
            if client_factory is not None:
                self._factory = client_factory
            todo = self._merged(plugin_services()) if services is None else services
            if services is None:
                self._pending = {}
            for service in todo:
                self._pending.pop(service, None)
            # Offers are re-recorded below from what each service offers now.
            for pid in [p for p, o in self.offered.items() if o["service"] in todo]:
                self.offered.pop(pid, None)
                self._retry.pop(pid, None)
            for service, (url, token) in todo.items():
                self._discover_one(service, url, token)
            self._next_discovery = time.monotonic() + _REDISCOVER_SECONDS

    def _discover_one(self, service: str, url: str, token: str) -> None:
        timeout = live_plugin_timeout()
        try:
            body = request(url, token, "GET", "/manifests", timeout=timeout,
                           factory=self._factory)
        except AdapterError as exc:
            # Down at boot is normal (containers start in any order): retry later.
            # The status says which: 503 not reachable, 401 token refused.
            self._pending[service] = (url, token)
            log.warning("plugin service not reachable yet; will retry %s",
                        kv(service=service, status=exc.status,
                           retry_seconds=_REDISCOVER_SECONDS))
            return
        offered = body.get("manifests")
        if not isinstance(offered, list):
            log.warning("plugin refused %s", kv(service=service, reason="malformed /manifests"))
            audit("system", "plugin.refused", service,
                  {"service": service, "reason": "malformed /manifests"}, result="denied")
            self._drop_unoffered(service, set())
            return
        log.info("plugin service discovered %s", kv(
            service=service, plugins=[m.get("id") for m in offered if isinstance(m, dict)]))
        registered: set[str] = set()
        for m in offered:
            pid = m.get("id") if isinstance(m, dict) else None
            vendored = self._safe_vendored(pid) if isinstance(pid, str) else None
            if vendored is None:
                self._pin(m, None, service)
                if isinstance(pid, str) and ID_RE.match(pid):
                    # Once the owner pins it, repin() builds the adapter here.
                    self._retry[pid] = lambda pinned, s=service, u=url, t=token: RemoteAdapter(
                        s, u, t, pinned, live_plugin_timeout, self._factory)
                continue
            adapter = RemoteAdapter(service, url, token, vendored, live_plugin_timeout,
                                    self._factory)
            if self.register(adapter, m, vendored):
                registered.add(vendored.id)
        self._drop_unoffered(service, registered)

    def _drop_unoffered(self, service: str, registered: set[str]) -> None:
        """A service's answer is the whole truth about it: a plugin it served
        before and did not (validly) offer now stops being served. Without
        this, a plugin restarted under a new container (an upgrade, failed or
        not, while the broker keeps running) could stay registered under a
        manifest that no longer matches its offer or its pin; a refused offer
        is kept in `offered` by _pin for the owner to review."""
        for pid in sorted(p for p, e in self._entries.items()
                          if e.service == service and p not in registered):
            self._entries.pop(pid, None)
            self._raw_offers.pop(pid, None)
            self._anc_cache.clear()
            log.warning("plugin no longer served: its service does not offer it as pinned %s",
                        kv(plugin=pid, service=service))

    def pending_services(self) -> list[str]:
        """Configured services not reached yet (retried on a later call)."""
        with self._lock:
            return sorted(self._pending)

    def discover_named(self, names: Iterable[str]) -> None:
        """Discover these services now, with their current values (after an
        install or upgrade job ended: the container is new). Unknown names
        are skipped."""
        with self._lock:
            current = self._merged(plugin_services())
            todo = {s: current[s] for s in names if s in current}
            if todo:
                self.discover(todo)

    def _maybe_rediscover(self) -> None:
        if not (self._pending and time.monotonic() >= self._next_discovery):
            return
        with self._lock:
            # Checked again under the lock: requests that queued behind one
            # rediscovery must not each run another.
            if not (self._pending and time.monotonic() >= self._next_discovery):
                return
            # A pending service is retried with its CURRENT values (a token
            # the installer changed since it failed); a service known to
            # neither source (an explicit discover() call) keeps its own.
            current = self._merged(plugin_services())
            self.discover({s: current.get(s, v) for s, v in self._pending.items()})

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
        # A snapshot (one C-level copy): the installer sync may evict a
        # service from another thread while this iterates.
        owners = [e for e in list(self._entries.values())
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
        ttl = runtime_settings().ancestors_cache_seconds     # a DB read: outside the lock
        with self._lock:
            self._anc_cache[key] = (now + ttl, chain)
        return chain

    def clear_ancestry_cache(self) -> None:
        """Forget cached parent chains (after the owner hides a resource)."""
        with self._lock:
            self._anc_cache.clear()

    def clear_cache(self) -> None:
        """Forget everything process-local a discovery rebuilds: the ancestry
        cache and the refused offers awaiting review."""
        with self._lock:
            self._anc_cache.clear()
            self.offered.clear()
            self._retry.clear()


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
