"""Owner operations on plugins: config, enable/disable, health, connect relay.

Every function takes the acting `AdminContext` and audits under the owner's
username. The broker relays secrets and authorization codes to the plugin
service exactly once and keeps neither: audit details name which fields
changed, never their values.

Enable = validate required config -> relay config to the plugin's
`/configure` -> store its `/status` as last_health. An enabled plugin that
reports unhealthy is legitimate (WhatsApp before pairing). A configure that
fails refuses the enable: an unconfigured plugin must not go live.

OAuth redirect URI: the broker computes it, because only the broker knows
how the owner reaches it. Public mode: `https://<SITE_DOMAIN>/oauth/
callback/<service>` (SITE_DOMAIN is a fail-closed exposure setting; a
missing one refuses the connect). Local mode: `http://<request host>/oauth/
callback/<service>`. The plugin stores it beside its state nonce and reuses
it for the code exchange.

Pins (plugins/pins.py): a plugin not vendored in the broker tree is
registered only once the owner pins its manifest. `offered()` lists every
refused offer with its review card; `pin()` approves what a running service
offers, `pin_manifest()` approves a manifest text directly (the install flow
pins before the service exists), `unpin()` withdraws the approval and the
plugin with it. In-tree ids can never be pinned here: the tree wins.
"""

from __future__ import annotations

import logging
import os
import re

import yaml

from ..audit import audit
from ..config import get_settings
from ..errors import PolicyError
from ..logging_setup import kv
from ..plugins import manifest_view, pins, settings
from ..plugins.adapter import AdapterError
from ..plugins.manifest import ID_RE, Manifest, ManifestError, load_manifest_text
from ..plugins.registry import get_registry, plugin_rows

log = logging.getLogger(__name__)


def _audit(ctx, action: str, resource: str, detail: dict | None = None,
           result: str = "ok") -> None:
    audit(ctx.username, action, resource, detail, result,
          actor_principal=ctx.principal_id, actor_via=ctx.via)


def _by(ctx) -> dict:
    return {"by": ctx.username, "via": ctx.via}


def _entry(plugin_id: str):
    e = get_registry().entries().get(plugin_id)
    if e is None:
        raise PolicyError(404, "no such plugin", "not_found")
    return e


def _relay_error(exc: AdapterError) -> PolicyError:
    status = exc.status if exc.status in (400, 404, 409, 502, 503) else 502
    return PolicyError(status, f"plugin: {exc.message}")


def view(plugin_id: str) -> dict:
    e = _entry(plugin_id)
    row = plugin_rows().get(plugin_id, {})
    m = e.manifest
    return {
        "id": m.id, "display_name": m.display_name, "description": m.description,
        "version": m.version, "service": e.service,
        "enabled": row.get("enabled") == 1, "connected": row.get("connected") == 1,
        "last_health": row.get("last_health", {}),
        "config": settings.effective_config(m, row.get("config", {})),
        "config_schema": settings.schema_view(m),
        "connection": {"kind": m.connection.kind, "enforcement": m.connection.enforcement,
                       "shared": m.connection.shared},
        "actions": sorted(m.action_names),
        # What the console derives its editors and approval cards from.
        "manifest": manifest_view.admin_view(m),
    }


def list_plugins() -> dict:
    reg = get_registry()
    return {"items": [view(pid) for pid in sorted(reg.entries())],
            "refused": [{"id": k, "reason": v} for k, v in sorted(reg.refused.items())]}


def refresh_health(plugin_id: str) -> dict:
    adapter = _entry(plugin_id).adapter
    try:
        status = adapter.status()
    except AdapterError as exc:
        # Unhealthy, `connected` unchanged, last reported enforcement kept.
        return settings.set_health_failure(plugin_id, exc.message, exc.status)
    settings.set_health(plugin_id, status, status.get("connected") is True)
    return status


def patch_config(ctx, plugin_id: str, patch: dict) -> dict:
    e = _entry(plugin_id)
    updates, secrets = settings.split_patch(e.manifest, patch)
    stored = settings.merge(plugin_rows().get(plugin_id, {}).get("config", {}), updates)
    enabled = plugin_rows().get(plugin_id, {}).get("enabled") == 1
    # A shared field lives in the service's one connection, which serves the
    # enabled siblings too: relay it even when this plugin is disabled.
    shared = any(k in settings.shared_names(e.manifest) for k in updates)
    if secrets or enabled or shared:
        # Relay first: if the plugin refuses, nothing is stored broker-side.
        try:
            e.adapter.configure(settings.effective_config(e.manifest, stored), secrets)
        except AdapterError as exc:
            _audit(ctx, "plugin.config", plugin_id, {"fields": sorted(patch)}, "error")
            log.warning("plugin configure refused by the plugin %s", kv(
                plugin=plugin_id, status=exc.status, fields=sorted(patch), **_by(ctx)))
            raise _relay_error(exc) from exc
    settings.store_config(plugin_id, stored)
    _propagate_shared(e.manifest, updates)
    _audit(ctx, "plugin.config", plugin_id,
           {"fields": sorted(updates), "secret_fields": sorted(secrets)})
    # Field NAMES only; a secret's value went to the plugin and nowhere else.
    log.info("plugin configured %s", kv(plugin=plugin_id, fields=sorted(updates),
                                        secret_fields=sorted(secrets),
                                        relayed=bool(secrets or enabled or shared), **_by(ctx)))
    return view(plugin_id)


def _propagate_shared(manifest, updates: dict) -> None:
    """Keep a shared non-secret value (the Google OAuth client id) the same
    on every plugin of the slot, so each one's view, enable and /configure
    carry it. The plugin service holds one connection, so nothing else
    needs relaying."""
    common = {k: v for k, v in updates.items() if k in settings.shared_names(manifest)}
    if not common:
        return
    rows = plugin_rows()
    for pid in settings.shared_siblings(manifest, get_registry().manifests()):
        settings.store_config(pid, settings.merge(rows.get(pid, {}).get("config", {}), common))


def enable(ctx, plugin_id: str) -> dict:
    e = _entry(plugin_id)
    config = settings.effective_config(e.manifest,
                                       plugin_rows().get(plugin_id, {}).get("config", {}))
    missing = settings.missing_required(e.manifest, config)
    if missing:
        raise PolicyError(400, f"required config missing: {missing}", "invalid_config")
    try:
        e.adapter.configure(config, {})
    except AdapterError as exc:
        _audit(ctx, "plugin.enable", plugin_id, {"error": exc.status}, "error")
        log.warning("plugin enable refused: configure failed %s",
                    kv(plugin=plugin_id, status=exc.status, **_by(ctx)))
        raise _relay_error(exc) from exc
    health = refresh_health(plugin_id)
    settings.set_enabled(plugin_id, True)
    _audit(ctx, "plugin.enable", plugin_id, {"healthy": health.get("healthy")})
    log.info("plugin enabled %s", kv(plugin=plugin_id, healthy=health.get("healthy") is True,
                                     connected=health.get("connected") is True,
                                     enforcement=health.get("enforcement"), **_by(ctx)))
    return view(plugin_id)


def disable(ctx, plugin_id: str) -> dict:
    _entry(plugin_id)
    settings.set_enabled(plugin_id, False)
    _audit(ctx, "plugin.disable", plugin_id)
    log.info("plugin disabled %s", kv(plugin=plugin_id, **_by(ctx)))
    return view(plugin_id)


def health(ctx, plugin_id: str) -> dict:
    refresh_health(plugin_id)
    return view(plugin_id)


# ---- connect relay ------------------------------------------------------------------

def _connect_target(name: str):
    """Accept a plugin id or a service name (the OAuth callback is per
    service: one Google consent covers gmail, gcal and gdrive)."""
    reg = get_registry()
    entries = reg.entries()
    if name in entries:
        e = entries[name]
    else:
        e = next((x for pid, x in sorted(entries.items()) if x.service == name), None)
        if e is None:
            raise PolicyError(404, "no such plugin or service", "not_found")
    siblings = sorted(pid for pid, x in entries.items() if x.service == e.service)
    return e, siblings


_HOST_RE = re.compile(r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
                      r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*"
                      r"|\[[0-9A-Fa-f:.]+\])(?::[0-9]{1,5})?")
_SERVICE_RE = re.compile(r"[a-z][a-z0-9]*")
# Connection kinds (the manifest's closed vocabulary) whose connect flow is
# an OAuth redirect and cannot start without a redirect URI.
_REDIRECT_KINDS = frozenset({"google_oauth"})


def oauth_redirect_uri(service: str, request_host: str) -> str:
    """Where Google sends the owner back: the broker's callback page."""
    if not _SERVICE_RE.fullmatch(service or ""):
        raise PolicyError(400, "this service name cannot have an OAuth callback", "bad_request")
    if get_settings().public_mode():
        # An exposure setting read from the environment only: a hijacked
        # console session must not be able to point the redirect elsewhere.
        domain = os.environ.get("SITE_DOMAIN", "").strip().lower()
        if not _HOST_RE.fullmatch(domain):
            raise PolicyError(409, "SITE_DOMAIN is not set on the broker; the OAuth redirect "
                                   "URI cannot be built", "not_configured")
        return f"https://{domain}/oauth/callback/{service}"
    host = (request_host or "").strip()
    if not _HOST_RE.fullmatch(host):
        raise PolicyError(400, "cannot build the OAuth redirect URI from this request's Host",
                          "bad_request")
    return f"http://{host}/oauth/callback/{service}"


def connect_start(ctx, name: str, request_host: str = "") -> dict:
    e, siblings = _connect_target(name)
    enabled = [pid for pid in get_registry().enabled_plugins() if pid in siblings]
    try:
        redirect = oauth_redirect_uri(e.service, request_host)
    except PolicyError:
        if e.manifest.connection.kind in _REDIRECT_KINDS:
            raise                          # OAuth cannot start without one
        redirect = None                    # QR / install flows never use it
    try:
        out = e.adapter.connect_start(enabled, redirect)
    except AdapterError as exc:
        log.warning("plugin connect start failed %s",
                    kv(service=e.service, status=exc.status, **_by(ctx)))
        raise _relay_error(exc) from exc
    _audit(ctx, "plugin.connect_start", e.service, {"plugins": enabled})
    # The kind only: an OAuth URL carries the state nonce.
    log.info("plugin connect started %s", kv(
        service=e.service, connection=e.manifest.connection.kind,
        kind=out.get("kind") if isinstance(out, dict) else None, plugins=enabled, **_by(ctx)))
    return out


def connect_finish(ctx, name: str, code: str | None, state: str | None,
                   installation_id: str | None) -> dict:
    e, siblings = _connect_target(name)
    try:
        out = e.adapter.connect_finish(code, state, installation_id)
    except AdapterError as exc:
        _audit(ctx, "plugin.connect_finish", e.service, {"error": exc.status}, "error")
        log.warning("plugin connect finish failed %s",
                    kv(service=e.service, status=exc.status, **_by(ctx)))
        raise _relay_error(exc) from exc
    for pid in siblings:
        refresh_health(pid)
    # The authorization code is relayed once and recorded nowhere.
    _audit(ctx, "plugin.connect_finish", e.service,
           {"installation": installation_id is not None})
    log.info("plugin connect finished %s",
             kv(service=e.service, installation=installation_id is not None, **_by(ctx)))
    return out


def connect_qr(ctx, name: str) -> bytes:
    e, _ = _connect_target(name)
    try:
        return e.adapter.connect_qr()
    except AdapterError as exc:
        raise _relay_error(exc) from exc


def disconnect(ctx, name: str) -> dict:
    e, siblings = _connect_target(name)
    try:
        out = e.adapter.disconnect()
    except AdapterError as exc:
        log.warning("plugin disconnect failed %s",
                    kv(service=e.service, status=exc.status, **_by(ctx)))
        raise _relay_error(exc) from exc
    for pid in siblings:
        refresh_health(pid)
    _audit(ctx, "plugin.disconnect", e.service)
    log.info("plugin disconnected %s", kv(service=e.service, **_by(ctx)))
    return out


def resolve(ctx, plugin_id: str, kind: str, query: str, limit: int = 20) -> list[dict]:
    """Admin-side name lookup for pickers (no visibility applied: the owner
    sees everything, including hidden resources, to manage them)."""
    e = _entry(plugin_id)
    if kind not in e.manifest.resources:
        raise PolicyError(400, f"unknown resource kind {kind!r}", "bad_request")
    try:
        return e.adapter.resolve(kind, query, max(1, min(limit, 50)))
    except AdapterError as exc:
        raise _relay_error(exc) from exc


# ---- pins: the owner approves an external plugin's manifest -------------------------

# Words the admin routes use right after /v1/admin/plugins/. A plugin with one
# of these ids would have its GET view shadowed by the route, so none can be
# pinned (and an id nobody can pin is never registered).
RESERVED_IDS = frozenset({"offered", "installed", "install"})


def _blocker(plugin_id: str, service: str | None) -> str | None:
    """Why `plugin_id` cannot be pinned (from `service`), or None."""
    reg = get_registry()
    if not isinstance(plugin_id, str) or not ID_RE.match(plugin_id):
        return "not a valid plugin id"
    if plugin_id in RESERVED_IDS:
        return f"{plugin_id!r} is a reserved word in the admin routes"
    try:
        in_tree = reg.in_tree(plugin_id) is not None
    except ManifestError:
        in_tree = True                     # an invalid in-tree file still owns the id
    if in_tree:
        # The tree wins over any row, so a pin here would never apply.
        return "this plugin ships with the broker; it is pinned by the broker's own tree"
    entry = reg.entries().get(plugin_id)
    if service is not None and entry is not None and entry.service != service:
        return f"plugin id already served by {entry.service!r}"
    return None


def pin_blocker(plugin_id: str, service: str | None = None) -> str | None:
    """Why `plugin_id` (hosted by `service`) cannot be pinned, or None. The
    install flow checks every id of a package with it before pinning any."""
    return _blocker(plugin_id, service)


def _offered_manifest(plugin_id: str, offer: dict) -> tuple[str, Manifest]:
    """The offered manifest as YAML text plus its validated form. The text
    is what is stored and what the broker then uses, never the raw offer."""
    raw = offer.get("manifest")
    if not isinstance(raw, dict):
        raise ManifestError("the offered manifest is not an object")
    text = yaml.safe_dump(raw, sort_keys=False, allow_unicode=True)
    m = load_manifest_text(text)
    if m.id != plugin_id:
        raise ManifestError(f"manifest declares id {m.id!r}, not {plugin_id!r}")
    return text, m


def _current_pin(plugin_id: str) -> Manifest | None:
    try:
        return pins.get(plugin_id)
    except ManifestError:
        return None                        # an unreadable pin diffs as a first pin


def _offer_view(plugin_id: str, offer: dict) -> dict:
    out = {"id": plugin_id, "service": offer.get("service"), "reason": offer.get("reason"),
           "pinned": pins.record(plugin_id), "valid": False, "error": None,
           "summary": None, "diff": None, "pinnable": False, "blocked": None}
    try:
        _, m = _offered_manifest(plugin_id, offer)
    except ManifestError as exc:
        out["error"] = str(exc)[:1000]
        return out
    blocked = _blocker(plugin_id, offer.get("service"))
    out.update(valid=True, summary=pins.summary(m), diff=pins.diff(_current_pin(plugin_id), m),
               pinnable=blocked is None, blocked=blocked)
    return out


def offered() -> dict:
    """Every refused offer, with what pinning it would approve."""
    return {"items": [_offer_view(pid, offer)
                      for pid, offer in sorted(get_registry().offers().items())]}


def pin_manifest(ctx, plugin_id: str, manifest_text: str, *, source: str = "",
                 ref: str = "", commit: str = "", service: str | None = None) -> dict:
    """Approve `manifest_text` as the pin for `plugin_id` (validated), audit
    it, and register the plugin now if its service already offered it."""
    blocked = _blocker(plugin_id, service)
    if blocked:
        raise PolicyError(409, blocked, "conflict")
    previous = pins.record(plugin_id)
    try:
        m = pins.set(plugin_id, manifest_text, source, ref, commit, ctx.username)
    except ManifestError as exc:
        _audit(ctx, "plugin.pin", plugin_id, {"error": "invalid manifest"}, "error")
        log.warning("plugin pin refused: invalid manifest %s", kv(plugin=plugin_id, **_by(ctx)))
        raise PolicyError(400, f"invalid manifest: {exc}"[:2000], "invalid_manifest") from exc
    registered = get_registry().repin(plugin_id)
    detail = {"version": m.version, "previous": previous["version"] if previous else None,
              "service": service, "source": source, "ref": ref, "commit": commit,
              "registered": registered}
    _audit(ctx, "plugin.pin", plugin_id, detail)
    log.info("plugin pinned %s", kv(plugin=plugin_id, version=m.version,
                                    previous=detail["previous"], service=service,
                                    registered=registered, **_by(ctx)))
    return {"pin": pins.record(plugin_id), "registered": registered,
            "plugin": view(plugin_id) if registered else None}


def pin(ctx, plugin_id: str) -> dict:
    """Approve the manifest a running service offered under `plugin_id`."""
    offer = get_registry().offers().get(plugin_id)
    if offer is None:
        raise PolicyError(404, "no offered manifest for this plugin id", "not_found")
    blocked = _blocker(plugin_id, offer.get("service"))
    if blocked:
        raise PolicyError(409, blocked, "conflict")
    try:
        text, _ = _offered_manifest(plugin_id, offer)
    except ManifestError as exc:
        _audit(ctx, "plugin.pin", plugin_id, {"error": "invalid manifest"}, "error")
        log.warning("plugin pin refused: invalid manifest %s", kv(plugin=plugin_id, **_by(ctx)))
        raise PolicyError(400, f"invalid manifest: {exc}"[:2000], "invalid_manifest") from exc
    return pin_manifest(ctx, plugin_id, text, service=offer.get("service"))


def unpin(ctx, plugin_id: str) -> dict:
    """Remove the owner's pin. The plugin stops being served at once (its
    offer goes back up for review) and its row is disabled, so a later pin
    needs an explicit enable."""
    record = pins.record(plugin_id)
    if record is None:
        raise PolicyError(404, "no pin for this plugin id", "not_found")
    pins.delete(plugin_id)
    reg = get_registry()
    try:
        in_tree = reg.in_tree(plugin_id) is not None
    except ManifestError:
        in_tree = True
    withdrawn = False
    if not in_tree:                        # an in-tree plugin never depended on the row
        withdrawn = reg.withdraw(plugin_id, "not pinned: the owner removed the pin")
        settings.set_enabled(plugin_id, False)
    _audit(ctx, "plugin.unpin", plugin_id, {"version": record["version"],
                                            "withdrawn": withdrawn})
    log.info("plugin unpinned %s", kv(plugin=plugin_id, version=record["version"],
                                      withdrawn=withdrawn, **_by(ctx)))
    return {"unpinned": plugin_id, "version": record["version"], "withdrawn": withdrawn}
