"""The agent skill doc, rendered from plugin manifests.

One renderer serves three uses:

  * `GET /skill` (and `/skill.md`): every enabled plugin, for any agent;
  * `GET /v1/me/skill` and the MCP resource `broker://skill`: filtered to one
    key (`KeyContext`), so actions the key cannot reach are left out and a
    "Your current capabilities" block (from get_my_access, which never lists
    hidden resources or deny sets) is added;
  * `aab skill build`: every vendored manifest, with YAML frontmatter, written
    to integrations/claude-skill/agent-authority-broker/SKILL.md, which CI
    re-renders and diffs.

`render` is pure: it takes manifests and, for a key, a precomputed
KeyContext, and touches no database or registry. That is what lets the CLI
build the committed file offline and makes the drift check deterministic.
Manifests are data: nothing here names a plugin.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..plugins.manifest import Manifest, load_manifest
from . import sections

# Vendored manifests live next to the registry's copy of the same path
# (plugins/registry.TARGETS_DIR); repeated here so the CLI can render without
# importing the registry (and with it the database layer).
TARGETS_DIR = Path(__file__).resolve().parents[1] / "targets"
SKILL_NAME = "agent-authority-broker"

_HOST_RE = re.compile(r"^(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:.]+\])(?::\d{1,5})?$")


@dataclass(frozen=True)
class KeyContext:
    """What the doc needs to know about one key, computed by the caller."""
    reachable: Mapping[str, set[str]]              # services/agent.reachable_actions
    access: Mapping[str, Any] = field(default_factory=dict)   # get_my_access


def _sorted(manifests: Mapping[str, Manifest] | Iterable[Manifest]) -> list[Manifest]:
    ms = manifests.values() if isinstance(manifests, Mapping) else manifests
    return sorted(ms, key=lambda m: m.id)


def render(base_url: str, manifests: Mapping[str, Manifest] | Iterable[Manifest],
           key_ctx: KeyContext | None = None, *, include_mcp: bool = True) -> str:
    """The skill doc (Markdown). Without `key_ctx`: every given manifest, all
    actions. With it: only actions in `key_ctx.reachable`, targets with none
    reduced to a name, plus the key's current capabilities."""
    ms = _sorted(manifests)
    base = base_url.rstrip("/") or sections.PLACEHOLDER
    filtered = key_ctx is not None
    reach = {pid: set(acts) for pid, acts in (key_ctx.reachable if filtered else {}).items()}
    shown = [m for m in ms if not filtered or reach.get(m.id)]
    unreachable = [m.id for m in ms if filtered and not reach.get(m.id)]
    only = reach if filtered else None
    parts = [
        sections.header(base, ms, key_ctx.access.get("name") if filtered else None),
        sections.connection(base, shown, only),
        sections.authority_model(base, shown, only),
    ]
    if filtered:
        parts.append(sections.your_capabilities(key_ctx.access))
    parts.append(sections.targets_intro(base, filtered, unreachable))
    parts += [sections.plugin(m, base, reach.get(m.id) if filtered else None) for m in shown]
    parts += [sections.rest_reference(base), sections.errors()]
    if include_mcp:
        parts.append(sections.mcp(base))
    return sections.join(parts) + "\n"


def frontmatter(manifests: Mapping[str, Manifest] | Iterable[Manifest]) -> str:
    listed = sections.listing([m.display_name for m in _sorted(manifests)]) or \
        "the owner's systems"
    description = (f"Read and act in the user's {listed} through the Agent Authority "
                   "Broker, which checks every call against what your aab_ agent key may "
                   "do. Use whenever the user asks you to read, search, send or change "
                   "something there. Needs the broker's base URL and an agent key.")
    return f"---\nname: {SKILL_NAME}\ndescription: {description}\n---\n\n"


def skill_file(manifests: Mapping[str, Manifest] | Iterable[Manifest],
               base_url: str = sections.PLACEHOLDER) -> str:
    """The committed SKILL.md: frontmatter + the full doc."""
    return frontmatter(manifests) + render(base_url, manifests)


def vendored_manifests(targets_dir: Path = TARGETS_DIR) -> dict[str, Manifest]:
    """Every vendored manifest (targets/<id>/manifest.yaml), enabled or not.
    The committed skill file is built from these, never from the database."""
    out = {}
    for path in sorted(Path(targets_dir).glob("*/manifest.yaml")):
        m = load_manifest(path)
        out[m.id] = m
    return out


def base_url_from(headers: Mapping[str, str], scheme: str) -> str:
    """The broker's public base URL as the caller reached it (Host, and
    X-Forwarded-Proto behind the edge). Anything that does not look like a
    host name falls back to the placeholder rather than being pasted into
    the doc: the Host header is caller-controlled."""
    host = (headers.get("host") or "").strip()
    proto = (headers.get("x-forwarded-proto") or scheme or "").split(",")[0].strip().lower()
    if proto not in ("http", "https"):
        proto = "http"
    if not host or not _HOST_RE.match(host):
        return sections.PLACEHOLDER
    return f"{proto}://{host}"
