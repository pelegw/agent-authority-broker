"""The owner ceiling P(principal): the most any key of that principal can do.

P is virtual, never stored: one all-"*", direct, unexpiring, unbudgeted
capability per plugin that is enabled AND connected, recomputed on every
evaluation. Disabling or disconnecting a plugin therefore removes it from
every key's effective set instantly, with no writes to any grant.

The plugin state comes from an injected iterable of
(manifest, enabled, connected) so this module stays pure; phase 3's plugin
registry supplies the real one.
"""

from collections.abc import Iterable

from ..plugins.manifest import Manifest
from .capability import Capability

PluginState = tuple[Manifest, bool, bool]


def ceiling(principal_id: str, plugin_states: Iterable[PluginState]) -> list[Capability]:
    """P for a principal. principal_id is unused while there is a single owner
    (every plugin belongs to them); it is in the signature so per-principal
    ceilings are an additive change."""
    del principal_id
    out = []
    for manifest, enabled, connected in plugin_states:
        if _on(enabled) and _on(connected):
            out.append(Capability(manifest.id, manifest.action_names, mode="direct"))
    return sorted(set(out))


def _on(flag) -> bool:
    """True for True or the sqlite integer 1, nothing else: a stray value
    (say the string "0" from a config blob) must not count as enabled."""
    return isinstance(flag, (bool, int)) and flag == 1
