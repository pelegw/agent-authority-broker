"""Deny-set input: validate a `{target: {kind: [ids]}}` set and normalize ids.

Shared by the owner's key editor (services/admin.py) and an agent's
`delegate` (services/delegation.py), so a deny typed by either matches the id
the engine compares: each id goes through the owning plugin's normalizer
(e.g. " R1" -> "r1"); a deny stored un-normalized would silently never match.

Agent input is `strict`: every target must be a registered plugin and every
kind one of its declared resources, and the set is size-capped. A deny can
only ever narrow, so this is hygiene rather than a safety check, but an agent
should not be able to park arbitrary data on a key row.
"""

from __future__ import annotations

from ..authority.denies import parse_denies
from ..errors import PolicyError
from ..plugins.adapter import AdapterError
from ..plugins.registry import get_registry

# Largest deny set an agent may attach to a key it delegates.
MAX_AGENT_DENY_IDS = 500


def normalize_denies(raw, *, strict: bool = False) -> dict:
    """Validate a deny set and normalize ids through each plugin. Raises a
    400 PolicyError (`invalid_denies`) on anything malformed."""
    try:
        denies = parse_denies(raw or {})
    except ValueError as exc:
        raise PolicyError(400, str(exc), "invalid_denies") from exc
    reg = get_registry()
    manifests = reg.manifests()
    if strict:
        total = sum(len(ids) for kinds in denies.values() for ids in kinds.values())
        if total > MAX_AGENT_DENY_IDS:
            raise PolicyError(400, f"at most {MAX_AGENT_DENY_IDS} denied ids",
                              "invalid_denies")
    out: dict = {}
    for target, kinds in denies.items():
        manifest, adapter = manifests.get(target), reg.adapter(target)
        if strict and manifest is None:
            raise PolicyError(400, f"denies: unknown target {target!r}", "invalid_denies")
        for kind, ids in kinds.items():
            res = manifest.resources.get(kind) if manifest else None
            if strict and res is None:
                raise PolicyError(400, f"denies.{target}: unknown resource kind {kind!r}",
                                  "invalid_denies")
            if res is not None and res.normalize and adapter is not None:
                try:
                    ids = sorted({adapter.normalize(kind, i) for i in ids})
                except AdapterError as exc:
                    if exc.status != 400:
                        # The plugin could not answer: storing the raw id would
                        # silently never match, so refuse as "try again later".
                        raise PolicyError(503, "plugin unavailable; cannot normalize the "
                                               "denied ids", "unavailable") from exc
                    raise PolicyError(400, f"denies.{target}.{kind}: {exc.message}",
                                      "invalid_denies") from exc
            out.setdefault(target, {})[kind] = ids
    return out
