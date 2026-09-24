"""`evaluate()`: may this key do this action on this resource, right now?

Pure decision, no side effects and no recording (engine.py records every
decision before acting). Order, each step failing closed:

  1. the plugin is registered AND enabled, else a 404-shaped deny (a
     disabled plugin looks exactly like one that does not exist);
  2. the action exists (404) and its params validate (400); scheduling and
     `as_draft` are only accepted where the manifest allows them (400);
  3. the plugin is connected (else 503: nothing could be performed);
  4. the selector param is normalized by the plugin;
  5. the key's effective capabilities are computed live (with their grant
     chains); the first capability covering target + action + resource
     wins, direct before draft. A hidden or denied resource (or one under a
     hidden folder) is then a 404 identical to missing. The hidden check
     comes after coverage on purpose: without authority the answer is the
     same 403 whether the resource is hidden or does not exist, so the
     status can never reveal that a hidden resource exists. A capability only covers an action whose
     modes include the mode it would run at (a draft-only authority cannot
     reach an action that cannot be drafted), and a selector dimension only
     restricts the actions in its `applies_to`. Dimensions the broker cannot
     check itself (list reads, a room restriction on an item) become
     `allow_only` in the CallScope for the plugin to enforce;
  6. none covers: 403 `out_of_grant`. Capability mode draft, or the caller's
     `as_draft`: draft. Otherwise allow, with `enforced_where` per bounding
     dimension from the manifest and the connection's live mode.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from .authority.capability import MODE_RANK, Capability
from .authority.effective import effective_with_chains
from .hidden import allow_only, deny_sets, is_denied
from .plugins.adapter import AdapterError, CallScope
from .plugins.manifest import SET_FORMS, Action, Manifest
from .plugins.registry import get_registry

NOT_FOUND = "not found"          # one message for missing AND hidden resources


@dataclass(frozen=True)
class Decision:
    decision: str                    # allow | draft | deny
    status: int                      # 200 allow, 202 draft, or the deny's HTTP status
    reason: str                      # machine reason recorded in the decision row
    message: str = ""                # human text for a deny
    code: str = ""                   # error code for a deny
    hint: str | None = None
    cap: Capability | None = None
    grant_chain_ids: tuple[str, ...] = ()
    resource: str = ""
    enforced_where: dict = field(default_factory=dict)
    scope: CallScope | None = None
    params: dict = field(default_factory=dict)   # validated and normalized
    side_effect: str = ""


def _deny(status: int, reason: str, message: str, code: str, *, hint: str | None = None,
          resource: str = "", params: dict | None = None, side_effect: str = "") -> Decision:
    return Decision("deny", status, reason, message, code, hint, resource=resource,
                    params=params or {}, side_effect=side_effect)


def applies(item, action: str) -> bool:
    """Does a narrowing or constraint restrict this action?"""
    return item.applies_to == ["*"] or action in item.applies_to


def selector_dims(manifest: Manifest, action: str) -> dict[str, str]:
    """Set-valued, stored dimensions that restrict `action`, mapped to the
    resource kind their ids belong to (the dimension name when none)."""
    return {n.dimension: (n.resource or n.dimension) for n in manifest.narrowings
            if n.form in SET_FORMS and not n.derived_from and applies(n, action)}


def enforced_where(manifest: Manifest, action: str, live_health: dict) -> dict[str, str]:
    """Per bounding dimension: `target` when the target system itself enforces
    it (inside the credential), `proxy` when only our code does. A plugin
    whose connection reports proxy mode right now (e.g. a PAT fallback)
    downgrades every dimension to proxy."""
    proxy_only = manifest.connection.enforcement == "proxy" or \
        (live_health or {}).get("enforcement") == "proxy"
    out = {}
    for item in [*manifest.narrowings, *manifest.constraints]:
        name = getattr(item, "dimension", None) or item.name
        if applies(item, action):
            out[name] = "target" if item.enforcement == "target" and not proxy_only else "proxy"
    # Always broker-enforced, whatever the plugin.
    out.update({"mode": "proxy", "budget": "proxy", "hidden": "proxy"})
    return dict(sorted(out.items()))


def evaluate(auth, target: str, action: str, params: Any, now: int, *,
             as_draft: bool = False, scheduled: bool = False,
             request_id: str = "") -> Decision:
    reg = get_registry()
    if target not in reg.enabled_plugins():
        return _deny(404, "target_unavailable", "no such target", "not_found")
    manifest = reg.manifests()[target]
    adapter = reg.adapter(target)
    act = manifest.action(action)
    if act is None:
        return _deny(404, "unknown_action", "no such action", "not_found")
    if not isinstance(params, dict):
        return _deny(400, "invalid_params", "params must be an object", "invalid_params",
                     side_effect=act.side_effect)
    try:
        model = act.params_model.model_validate(params)
    except ValidationError as exc:
        return _deny(400, "invalid_params", _validation_summary(exc), "invalid_params",
                     side_effect=act.side_effect)
    clean = {k: v for k, v in model.model_dump(mode="json").items() if v is not None}
    if scheduled and not act.schedulable:
        return _deny(400, "not_schedulable", "this action cannot be scheduled",
                     "not_schedulable", side_effect=act.side_effect)
    if as_draft and "draft" not in act.effective_modes:
        return _deny(400, "draft_unsupported", "this action cannot be drafted",
                     "draft_unsupported", side_effect=act.side_effect)
    states = reg.plugin_states()
    if not any(m.id == target and connected for m, _, connected in states):
        return _deny(503, "not_connected", "target is not connected", "not_connected",
                     side_effect=act.side_effect)

    resource_id = ""
    if act.selector_param and act.selector_param in clean:
        normalized = _normalize(manifest, act, adapter, clean[act.selector_param])
        if isinstance(normalized, Decision):
            return normalized
        resource_id = clean[act.selector_param] = normalized

    lattice = reg.lattice()
    deny = deny_sets(auth, target)
    hidden_resource = bool(resource_id and act.resource and is_denied(
        deny.get(act.resource, set()), resource_id, act.resource, lattice.ancestors))

    dims = selector_dims(manifest, action)
    candidates = [(c, chain) for c, chain in effective_with_chains(auth, now, states, lattice)
                  if c.target == target and action in c.actions]
    # Direct authority first: the least interrupting capability that covers wins.
    candidates.sort(key=lambda cc: (-MODE_RANK[cc[0].mode], cc[0]))
    mode_blocked = False
    for cap, chain in candidates:
        mode = cap.mode
        if as_draft or "direct" not in act.effective_modes:
            mode = "draft"
        if mode not in act.effective_modes:
            mode_blocked = True
            continue
        if not _covers(cap, act, resource_id, dims, lattice):
            continue
        if hidden_resource:
            # Checked only once some capability covers the call: a key with
            # no authority here gets the same 403 for a hidden resource as
            # for a nonexistent one, and a key with authority the same 404.
            return _deny(404, "hidden", NOT_FOUND, "not_found", resource=resource_id,
                         params=clean, side_effect=act.side_effect)
        scope = CallScope(
            request_id=request_id,
            visibility=_visibility(manifest, deny, cap, dims),
            constraints={k: v for k, v in cap.constraints.items()
                         if _constraint_applies(manifest, k, action)},
            credential=_credential(manifest, act, cap, dims))
        return Decision(
            "draft" if mode == "draft" else "allow", 202 if mode == "draft" else 200,
            "as_draft" if as_draft else ("draft_mode" if mode == "draft" else "covered"),
            cap=cap, grant_chain_ids=chain, resource=resource_id,
            enforced_where=enforced_where(manifest, action, reg.last_health(target)),
            scope=scope, params=clean, side_effect=act.side_effect)
    if mode_blocked:
        return _deny(403, "mode_unsupported",
                     "your authority for this action is draft-only, and it cannot be drafted",
                     "out_of_grant", hint="request_permission", resource=resource_id,
                     params=clean, side_effect=act.side_effect)
    return _deny(403, "out_of_grant", "not covered by any of your grants", "out_of_grant",
                 hint="request_permission", resource=resource_id, params=clean,
                 side_effect=act.side_effect)


def _normalize(manifest: Manifest, act: Action, adapter, raw: Any) -> str | Decision:
    if not isinstance(raw, str) or not raw.strip():
        return _deny(400, "invalid_resource", f"{act.selector_param} must be a non-empty string",
                     "invalid_params", side_effect=act.side_effect)
    res = manifest.resources.get(act.resource or "")
    if res is None or not res.normalize:
        return raw.strip()
    try:
        return adapter.normalize(act.resource, raw)
    except AdapterError as exc:
        if exc.status == 400:
            return _deny(400, "invalid_resource", exc.message, "invalid_params",
                         side_effect=act.side_effect)
        if exc.status == 404:
            return _deny(404, "missing", NOT_FOUND, "not_found", side_effect=act.side_effect)
        # Normalization has no side effect, so any failure is "not performed".
        return _deny(503, "plugin_unavailable", "target temporarily unavailable",
                     "unavailable", side_effect=act.side_effect)


def _covers(cap: Capability, act: Action, resource_id: str, dims: dict[str, str],
            lattice) -> bool:
    """Does `cap` reach this resource? Only dimensions that restrict the
    action and address the action's own resource kind can be checked here;
    the rest are handed to the plugin as allow_only."""
    forms = lattice.forms.get(cap.target, {})
    for dim, ids in cap.selector.items():
        if dim not in dims:
            continue                       # dimension does not restrict this action
        if not resource_id or dims[dim] != act.resource:
            continue                       # enforced by the plugin via allow_only
        spec = forms.get(dim)
        if spec is None:
            return False                   # uninterpretable: never covers
        if spec.form == "subtree":
            if resource_id not in ids and not any(
                    a in ids for a in lattice.ancestors(spec.kind, resource_id)):
                return False
        elif resource_id not in ids:
            return False
    return True


def _visibility(manifest: Manifest, deny: dict[str, set[str]], cap: Capability,
                dims: dict[str, str]) -> dict[str, dict]:
    kinds = set(manifest.resources) | set(dims.values()) | set(deny)
    out = {}
    for kind in sorted(kinds):
        d = deny.get(kind, set())
        a = allow_only(cap, dims, kind)
        if d or a is not None:
            out[kind] = {"deny": sorted(d), "allow_only": sorted(a) if a is not None else None}
    return out


def _constraint_applies(manifest: Manifest, name: str, action: str) -> bool:
    for item in [*manifest.narrowings, *manifest.constraints]:
        if (getattr(item, "dimension", None) or item.name) == name:
            return applies(item, action)
    return False


def _credential(manifest: Manifest, act: Action, cap: Capability,
                dims: dict[str, str]) -> dict:
    """Requirements for the plugin's connection to mint exactly this much:
    the action's own target permissions, plus target-enforced selectors."""
    out: dict[str, Any] = {}
    if act.target_permissions:
        out["permissions"] = dict(sorted(act.target_permissions.items()))
    resources = {n.dimension: sorted(cap.selector[n.dimension]) for n in manifest.narrowings
                 if n.enforcement == "target" and n.dimension in dims
                 and n.dimension in cap.selector}
    if resources:
        out["resources"] = resources
    return out


def _validation_summary(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors()[:5]:
        loc = ".".join(str(x) for x in err.get("loc", ())) or "params"
        parts.append(f"{loc}: {err.get('msg', 'invalid')}")
    return "invalid params: " + "; ".join(parts)
