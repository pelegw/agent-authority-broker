"""Owner operations on plugins: config, enable/disable, health, connect relay.

Every function takes the acting `AdminContext` and audits under the owner's
username. The broker relays secrets and authorization codes to the plugin
service exactly once and keeps neither: audit details name which fields
changed, never their values.

Enable = validate required config -> relay config to the plugin's
`/configure` -> store its `/status` as last_health. An enabled plugin that
reports unhealthy is legitimate (WhatsApp before pairing). A configure that
fails refuses the enable: an unconfigured plugin must not go live.
"""

from __future__ import annotations

from ..audit import audit
from ..errors import PolicyError
from ..plugins import manifest_view, settings
from ..plugins.adapter import AdapterError
from ..plugins.registry import get_registry, plugin_rows


def _audit(ctx, action: str, resource: str, detail: dict | None = None,
           result: str = "ok") -> None:
    audit(ctx.username, action, resource, detail, result,
          actor_principal=ctx.principal_id, actor_via=ctx.via)


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
        "connection": {"kind": m.connection.kind, "enforcement": m.connection.enforcement},
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
    if secrets or enabled:
        # Relay first: if the plugin refuses, nothing is stored broker-side.
        try:
            e.adapter.configure(settings.effective_config(e.manifest, stored), secrets)
        except AdapterError as exc:
            _audit(ctx, "plugin.config", plugin_id, {"fields": sorted(patch)}, "error")
            raise _relay_error(exc) from exc
    settings.store_config(plugin_id, stored)
    _audit(ctx, "plugin.config", plugin_id,
           {"fields": sorted(updates), "secret_fields": sorted(secrets)})
    return view(plugin_id)


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
        raise _relay_error(exc) from exc
    health = refresh_health(plugin_id)
    settings.set_enabled(plugin_id, True)
    _audit(ctx, "plugin.enable", plugin_id, {"healthy": health.get("healthy")})
    return view(plugin_id)


def disable(ctx, plugin_id: str) -> dict:
    _entry(plugin_id)
    settings.set_enabled(plugin_id, False)
    _audit(ctx, "plugin.disable", plugin_id)
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


def connect_start(ctx, name: str) -> dict:
    e, siblings = _connect_target(name)
    enabled = [pid for pid in get_registry().enabled_plugins() if pid in siblings]
    try:
        out = e.adapter.connect_start(enabled)
    except AdapterError as exc:
        raise _relay_error(exc) from exc
    _audit(ctx, "plugin.connect_start", e.service, {"plugins": enabled})
    return out


def connect_finish(ctx, name: str, code: str | None, state: str | None,
                   installation_id: str | None) -> dict:
    e, siblings = _connect_target(name)
    try:
        out = e.adapter.connect_finish(code, state, installation_id)
    except AdapterError as exc:
        _audit(ctx, "plugin.connect_finish", e.service, {"error": exc.status}, "error")
        raise _relay_error(exc) from exc
    for pid in siblings:
        refresh_health(pid)
    # The authorization code is relayed once and recorded nowhere.
    _audit(ctx, "plugin.connect_finish", e.service,
           {"installation": installation_id is not None})
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
        raise _relay_error(exc) from exc
    for pid in siblings:
        refresh_health(pid)
    _audit(ctx, "plugin.disconnect", e.service)
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
