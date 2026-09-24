"""Markdown building blocks for the skill doc: curl calls, code blocks, times,
param signatures. Pure string functions shared by sections.py (the
guide-wide text) and plugin_section.py (one target's section)."""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Mapping
from typing import Any

from ..plugins.manifest import Action

AUTH_HEADER = "Authorization: Bearer $AAB_KEY"
PLACEHOLDER = "{{BASE_URL}}"
ACTIONS_PATH = "/v1/targets/{target}/actions/{action}"


def join(parts: Iterable[str]) -> str:
    return "\n\n".join(p.strip("\n") for p in parts if p and p.strip())


def _shell_json(value: Any) -> str:
    """JSON for a single-quoted shell argument."""
    return json.dumps(value, ensure_ascii=False).replace("'", "'\\''")


def curl(base: str, method: str, path: str, body: Any = None) -> str:
    if body is None:
        return f'curl -s {"" if method == "GET" else f"-X {method} "}{base}{path} -H "{AUTH_HEADER}"'
    return (f"curl -s -X {method} {base}{path} \\\n"
            f'  -H "{AUTH_HEADER}" -H "Content-Type: application/json" \\\n'
            f"  -d '{_shell_json(body)}'")


def code(text: str, lang: str = "bash") -> str:
    return f"```{lang}\n{text}\n```"


def when(ts: int | None) -> str:
    """A unix time for humans and agents alike (UTC, minute precision)."""
    if ts is None:
        return "never"
    return time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(ts))


def action_path(target: str, action: str) -> str:
    return ACTIONS_PATH.format(target=target, action=action)


def listing(names: list[str]) -> str:
    """"A", "A and B", "A, B and C"."""
    if len(names) <= 1:
        return names[0] if names else ""
    return ", ".join(names[:-1]) + " and " + names[-1]


def _bounds(lo, hi, unit: str = "") -> str:
    if lo is not None and hi is not None:
        return f"{lo}-{hi}{unit}"
    if lo is not None:
        return f">= {lo}{unit}"
    return f"<= {hi}{unit}"


def _param_type(spec: Mapping) -> str:
    if "enum" in spec:
        return " | ".join(str(v) for v in spec["enum"])
    kind = spec.get("type", "any")
    if kind == "array":
        kind = f"array of {(spec.get('items') or {}).get('type', 'any')}"
    lo, hi = spec.get("minimum"), spec.get("maximum")
    if lo is not None or hi is not None:
        kind += " " + _bounds(lo, hi)
    lo, hi = spec.get("minLength"), spec.get("maxLength")
    if (lo is not None and lo > 1) or hi is not None:
        kind += ", " + _bounds(lo, hi, " chars")
    return kind


def param_signature(act: Action) -> str:
    """Compact form for the action table: required params marked `*`."""
    props = act.params.get("properties") or {}
    if not props:
        return "none"
    required = set(act.params.get("required") or ())
    return ", ".join(f"`{n}{'*' if n in required else ''}`" for n in props)


def param_inline(act: Action) -> str:
    """Every param on one line: name (type, bounds; required | default)
    and its description."""
    props = act.params.get("properties") or {}
    required = set(act.params.get("required") or ())
    out = []
    for name, spec in props.items():
        detail = _param_type(spec)
        if name in required:
            detail += ", required"
        elif "default" in spec:
            detail += f", default {json.dumps(spec['default'], ensure_ascii=False)}"
        desc = (spec.get("description") or "").strip().rstrip(".")
        out.append(f"`{name}` ({detail}){': ' + desc if desc else ''}")
    return "; ".join(out)


def modes(act: Action) -> str:
    return ", ".join(act.effective_modes)


def controls(act: Action) -> list[str]:
    out = []
    if act.side_effect != "read" and "draft" in act.effective_modes:
        out.append("`as_draft`")
    if act.schedulable:
        out.append("`run_at` | `delay_seconds`")
    if out:
        out.append("`note`")
    return out
