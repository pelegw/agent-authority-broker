"""One target's section of the skill doc, rendered from its manifest.

Nothing here names a plugin: the section is the manifest's description,
where each limit is enforced (from `enforcement` per narrowing and
constraint), addressing, resources and grant dimensions, the action table
(REST first, then the MCP tool, side effect, params, modes, schedulability),
one line per action with its params, the manifest's rules, and its
examples as REST `curl` calls. A key's copy passes `reachable` so actions
(and examples) the key cannot reach are left out.
"""

from __future__ import annotations

from ..plugins.manifest import Manifest
from .markdown import (action_path, code, controls, curl, join, modes, param_inline,
                       param_signature)


def enforcement_note(m: Manifest) -> str:
    items = [*m.narrowings, *m.constraints]
    if m.connection.enforcement == "proxy":
        return ("Enforcement: every limit on this target is applied by the broker "
                "(`proxy`); the connection itself has the account's full access.")
    target = sorted(getattr(i, "dimension", None) or i.name for i in items
                    if i.enforcement == "target")
    proxy = sorted(getattr(i, "dimension", None) or i.name for i in items
                   if i.enforcement != "target")
    out = "Enforcement: "
    if target:
        out += (", ".join(f"`{d}`" for d in target) + " enforced by the target itself "
                "(`target`: the broker mints a credential limited to your grant)")
    if proxy:
        out += ("; " if target else "") + ", ".join(f"`{d}`" for d in proxy) + \
            " by the broker (`proxy`)"
    return out + (". A fallback connection may downgrade everything to `proxy`; "
                  "`enforced_where` reports it per call.")


_FORM_TEXT = {
    "list": "a list of {kind} ids",
    "subtree": "{kind} ids, each covering everything below it",
    "pattern": "exact-match patterns",
    "range": "an integer upper bound",
    "flag": "true or false",
}


def _dimension(name: str, form: str, kind: str | None, values: list[str] | None) -> str:
    if form == "level":
        return f"`{name}`: one of " + " < ".join(values or [])
    return f"`{name}`: " + _FORM_TEXT[form].format(kind=kind or name)


def _resources(m: Manifest) -> str:
    lines = []
    for kind, res in m.resources.items():
        bits = [f"`{kind}` ({res.display.lower()})"]
        if res.id_format:
            bits.append(f"id: {res.id_format}")
        if res.resolve:
            bits.append(f"look ids up with `GET /v1/targets/{m.id}/resolve?kind={kind}&q=...`")
        lines.append("- Resource " + "; ".join(bits) + ".")
    dims = [_dimension(n.dimension, n.form, n.resource, n.values)
            for n in m.narrowings if not n.derived_from]
    dims += [_dimension(c.name, c.form, None, c.values) for c in m.constraints]
    if dims:
        lines.append("- Capability `selector` / `constraints` for this target: "
                     + "; ".join(dims) + ".")
    return "\n".join(lines)


def plugin(m: Manifest, base: str, reachable: set[str] | None) -> str:
    """One target's section. `reachable` None = all actions (the full doc);
    otherwise only those actions appear, and examples for others are
    dropped."""
    actions = [a for a in m.actions if reachable is None or a.name in reachable]
    head = f"### {m.display_name} (`{m.id}`)"
    parts = [head, m.description.strip(), enforcement_note(m)]
    if m.skill.addressing.strip():
        parts.append("Addressing: " + m.skill.addressing.strip())
    parts.append(_resources(m))
    rows = ["| REST | MCP | Effect | Params | Modes | Schedulable |",
            "|---|---|---|---|---|---|"]
    for act in actions:
        rows.append(f"| `POST {action_path(m.id, act.name)}` | `{m.id}_{act.name}` | "
                    f"{act.side_effect} | {param_signature(act)} | {modes(act)} | "
                    f"{'yes' if act.schedulable else 'no'} |")
    parts.append("\n".join(rows))
    details = []
    for act in actions:
        notes = []
        if act.long_poll:
            notes.append(f"Long-poll (REST only): `GET {action_path(m.id, act.name)}"
                         "?<params>&wait=25` holds until something new arrives.")
        if act.returns == "binary":
            notes.append("Returns raw bytes with their content type (MCP: base64).")
        ctl = controls(act)
        if ctl:
            notes.append("Controls: " + ", ".join(ctl) + ".")
        line = f"- `{act.name}`: {act.doc.strip() or act.name.replace('_', ' ')}"
        params = param_inline(act)
        if params:
            line += f" Params: {params}."
        if notes:
            line += " " + " ".join(notes)
        details.append(line)
    parts.append("\n".join(details))
    if m.skill.rules:
        parts.append("Rules:\n" + "\n".join(f"- {r.strip()}" for r in m.skill.rules))
    examples = [ex for ex in m.skill.examples if reachable is None or ex.action in reachable]
    for ex in examples:
        parts.append(f"Example: {ex.title}\n"
                     + code(curl(base, "POST", action_path(m.id, ex.action),
                                 {"params": ex.params})))
    return join(parts)
