"""Plugin action tools for MCP: derived per caller from manifests, run via the engine.

Naming: the canonical action id is `<plugin>.<action>`, but MCP tool names
cannot contain ".", so the tool is `<plugin>_<action>`. Plugin ids match
`^[a-z][a-z0-9]*$` (no underscore), so splitting at the FIRST underscore
recovers (plugin, action) unambiguously.

The list is computed on every `tools/list` from enabled plugins intersected
with the caller's reach (services/agent.reachable_actions, the same set REST
`/v1/targets` reports). It is only an advertisement: every call goes through
`engine.perform`, which re-evaluates from scratch, so a stale tool name (the
plugin was disabled, a grant revoked since the list was fetched) is refused
exactly as the REST route would refuse it.

A tool's `inputSchema` is the action's params schema, flat, plus the call
controls the REST body carries at top level: `as_draft` (writes that can be
drafted), `run_at` / `delay_seconds` (schedulable actions) and `note`
(anything that can end up queued). If a manifest param happens to share a
control's name, the param wins and that control is not offered for the
action; nothing is ever silently reinterpreted.
"""

from __future__ import annotations

import base64
import copy
import json
from typing import Any

from mcp import types

from . import engine, mcp_generic
from .errors import PolicyError
from .plugins.manifest import ID_RE, NAME_RE, Action, Manifest
from .plugins.registry import get_registry
from .services import agent

_CONTROL_SCHEMAS = {
    "as_draft": {"type": "boolean", "default": False,
                 "description": "Queue for human approval instead of acting now."},
    "run_at": {"type": "integer", "description": "Unix time to run at (schedules it)."},
    "delay_seconds": {"type": "integer", "minimum": 1,
                      "description": "Run this many seconds from now (schedules it)."},
    "note": {"type": "string", "maxLength": 1000,
             "description": "Why you are doing this; shown to the approving human."},
}
CONTROLS = tuple(_CONTROL_SCHEMAS)


def tool_name(plugin: str, action: str) -> str:
    return f"{plugin}_{action}"


def split_tool_name(name: str) -> tuple[str, str] | None:
    plugin, sep, action = name.partition("_")
    if not sep or not ID_RE.match(plugin) or not NAME_RE.match(action):
        return None
    return plugin, action


def _param_names(act: Action | None) -> set[str]:
    return set((act.params.get("properties") or {}) if act else ())


def controls_for(act: Action) -> list[str]:
    """Controls this action accepts, in a stable order."""
    out = []
    drafts = act.side_effect != "read" and "draft" in act.effective_modes
    if drafts:
        out.append("as_draft")
    if act.schedulable:
        out += ["run_at", "delay_seconds"]
    if drafts or act.schedulable:
        out.append("note")                   # only a queued action carries a note
    taken = _param_names(act)
    return [c for c in out if c not in taken]


def _description(act: Action, controls: list[str]) -> str:
    parts = [act.doc.strip() or act.name.replace("_", " ")]
    if act.side_effect != "read":
        parts.append(f"Side effect: {act.side_effect}. May return "
                     '{"status": "pending_approval", "action_id"} when a human must '
                     "approve first; that is normal, not an error: poll get_action_status.")
    if "as_draft" in controls:
        parts.append("as_draft=true queues it for approval even when you could act directly.")
    if "run_at" in controls:
        parts.append('run_at or delay_seconds schedules it: {"status": "scheduled", '
                     '"action_id"}.')
    if act.returns == "binary":
        parts.append("Returns the content base64-encoded with its MIME type.")
    if act.long_poll:
        parts.append("Returns at once over MCP; to wait for news use REST GET "
                     "…/actions/{action}?wait=N.")
    return " ".join(parts)


def action_tool(manifest: Manifest, act: Action) -> types.Tool:
    controls = controls_for(act)
    schema = copy.deepcopy(act.params) or {}
    schema["type"] = "object"
    props = dict(schema.get("properties") or {})
    for c in controls:
        props[c] = dict(_CONTROL_SCHEMAS[c])
    schema["properties"] = props
    # The engine's params model refuses unknown keys; say so up front.
    schema.setdefault("additionalProperties", False)
    return types.Tool(
        name=tool_name(manifest.id, act.name),
        title=f"{manifest.display_name}: {act.name.replace('_', ' ')}",
        description=_description(act, controls),
        inputSchema=schema,
        annotations=types.ToolAnnotations(readOnlyHint=act.side_effect == "read",
                                          destructiveHint=act.side_effect == "destructive"))


def tools_for(auth) -> list[types.Tool]:
    """Generic tools, then one tool per action this key can reach on an
    enabled plugin. Blocking (reads the database); call from a thread."""
    tools = [g.tool() for g in mcp_generic.GENERIC]
    taken = {t.name for t in tools}
    manifests = get_registry().enabled_manifests()
    for pid, actions in sorted(agent.reachable_actions(auth).items()):
        m = manifests.get(pid)
        if m is None:
            continue
        for name in sorted(actions):
            act = m.action(name)
            # A generic tool name always wins; the plugin action stays
            # reachable over REST. (Unlikely: it needs a plugin id equal to
            # a generic tool's first word, e.g. a plugin called "list".)
            if act is None or tool_name(pid, name) in taken:
                continue
            tools.append(action_tool(m, act))
    return tools


# ---- calls ------------------------------------------------------------------------

def _split_controls(act: Action | None, arguments: dict) -> tuple[dict, dict]:
    """(params, controls). A control name that is not one of the action's
    params is a control even where the schema does not offer it, so an
    unsupported one gets the engine's precise refusal (draft_unsupported,
    not_schedulable) exactly as over REST, instead of a vaguer extra-param 400."""
    taken = _param_names(act)
    params, controls = {}, {}
    for k, v in arguments.items():
        (controls if k in CONTROLS and k not in taken else params)[k] = v
    as_draft = controls.get("as_draft", False)
    note = controls.get("note", "")
    ints = {k: controls.get(k) for k in ("run_at", "delay_seconds")}
    bad = [k for k, v in ints.items() if v is not None and (isinstance(v, bool)
                                                            or not isinstance(v, int))]
    if not isinstance(as_draft, bool):
        bad.append("as_draft")
    if not isinstance(note, str) or len(note) > 1000:
        bad.append("note")
    if bad:
        raise PolicyError(422, "invalid request: " + "; ".join(
            f"{k}: invalid value" for k in sorted(bad)), "invalid_request")
    return params, {"as_draft": as_draft, "note": note, **ints}


def _encode(result: engine.EngineResult, target: str, action: str) -> types.CallToolResult:
    if result.binary is not None:
        mime = result.mime or "application/octet-stream"
        data = base64.b64encode(result.binary).decode("ascii")
        if mime.startswith("image/"):
            block: Any = types.ImageContent(type="image", data=data, mimeType=mime)
        else:
            block = types.EmbeddedResource(type="resource", resource=types.BlobResourceContents(
                uri=f"broker://result/{target}/{action}", mimeType=mime, blob=data))
        return types.CallToolResult(content=[block], isError=False)
    return text_result(result.body)


def text_result(body: Any, *, error: bool = False) -> types.CallToolResult:
    """JSON text, compact like the REST bodies (agents pay per byte)."""
    text = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)],
                                isError=error)


def dispatch(auth, name: str, arguments: Any) -> types.CallToolResult:
    """Run tool `name` as `auth`. Raises PolicyError for every refusal; the
    caller turns it into an isError result. Blocking; call from a thread."""
    if not isinstance(arguments, dict):
        raise PolicyError(422, "invalid request: arguments must be an object",
                          "invalid_request")
    if name in mcp_generic.BY_NAME:
        return text_result(mcp_generic.call(auth, name, arguments))
    parsed = split_tool_name(name)
    if parsed is None:
        raise PolicyError(404, "no such tool", "not_found")
    target, action = parsed
    # All registered manifests, not only enabled ones: this is only for
    # telling params from controls; the engine decides whether the plugin
    # is enabled (404 if not, exactly as over REST).
    manifest = get_registry().manifests().get(target)
    params, controls = _split_controls(manifest.action(action) if manifest else None,
                                       arguments)
    result = engine.perform(auth, target, action, params, **controls)
    return _encode(result, target, action)
