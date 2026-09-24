"""What a plugin author implements: an adapter object and its connection.

The runtime (app.py) hosts one or more adapters, one per manifest id. It
never interprets a target; it only translates the plugin API's JSON into
calls on these objects and their exceptions into HTTP statuses.

`scope` arrives as the JSON dict the broker sent (see docs/plugin-api.md,
"CallScope"). The adapter must never return, act on, or acknowledge a
resource listed in `scope["visibility"][kind]["deny"]`, and when
`allow_only` is a list it must stay inside it. A get on a denied resource
raises AdapterError(404) exactly like a missing one.
"""

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class Result:
    """What `perform` returns: JSON `data`, or raw `binary` with a `mime` type."""
    data: Any = None
    binary: bytes | None = None
    mime: str | None = None


class SecretReader(Protocol):
    """Read-only view of the plugin's own secrets (written via /configure)."""
    def get(self, name: str, default: str | None = None) -> str | None: ...


@runtime_checkable
class Connection(Protocol):
    """The credential side of a plugin: connect flow and token minting."""
    def start(self, enabled_plugins: list[str]) -> dict: ...
    def finish(self, code: str | None, state: str | None,
               installation_id: str | None) -> dict: ...
    def qr_png(self) -> bytes: ...
    def disconnect(self) -> dict: ...
    def status(self) -> dict: ...
    def mint(self, requirements: dict) -> Any: ...


class PluginAdapter(Protocol):
    """One target. `manifest` is the manifest as a plain dict (from YAML)."""
    manifest: dict
    connection: Connection | None

    def configure(self, config: dict, secrets: SecretReader) -> None: ...
    def status(self) -> dict: ...
    def normalize(self, kind: str, value: str) -> str: ...
    def resolve(self, kind: str, query: str, limit: int) -> list[dict]: ...
    def label(self, kind: str, ids: list[str]) -> dict[str, str]: ...
    def perform(self, action: str, params: dict, scope: dict) -> Result: ...
    # Optional: `ancestors(kind, resource_id) -> list[str]` (nearest first),
    # needed only by plugins with a `subtree` narrowing.
