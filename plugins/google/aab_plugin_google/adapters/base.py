"""What gmail, gcal and gdrive share as `aab_plugin_runtime` adapters.

Each hosted plugin id is one adapter object over the SAME connection and
client (one Google account, one refresh token). The base class loads the
packaged manifest, registers its scopes with the connection, checks at boot
that the code implements exactly the manifest's actions, parses every
CallScope strictly (callscope.py) and dispatches.

Two kinds of token request exist, and they are kept apart on purpose:
  * a `/perform` runs on the broker's requirements (`scope.requirements`),
    i.e. exactly the scopes of the action the broker decided on;
  * lookups the broker makes without an agent call (normalize, resolve,
    label, ancestors: pickers, cards, the subtree lattice) run on the
    plugin's own read-only scope (`LOOKUP`), never on a write scope.
"""

import time
from collections.abc import Callable
from pathlib import Path

import yaml
from aab_plugin_runtime import AdapterError, Result

from ..callscope import CallScope, constraint_forms

MANIFEST_DIR = Path(__file__).resolve().parents[1] / "manifests"


class GoogleAdapter:
    plugin_id = ""
    LOOKUP: dict = {}                       # requirements for broker-side lookups
    RESOURCE_KINDS: tuple[str, ...] = ()

    def __init__(self, connection, client, *, clock: Callable[[], float] = time.time,
                 manifest_path: Path | None = None):
        path = manifest_path or MANIFEST_DIR / f"{self.plugin_id}.yaml"
        self.manifest = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        if self.manifest.get("id") != self.plugin_id:
            raise RuntimeError(f"manifest id {self.manifest.get('id')!r} is not {self.plugin_id!r}")
        self.connection = connection
        self.client = client
        self._clock = clock
        self._forms = constraint_forms(self.manifest)
        connection.register(self.manifest)
        self._actions: dict[str, Callable[[dict, CallScope], Result]] = self.handlers()
        # The manifest is the contract the broker enforces against; an action
        # it declares that this code does not implement (or the reverse) is a
        # packaging bug, so the container refuses to start.
        declared = {a["name"] for a in self.manifest.get("actions", [])}
        if declared != set(self._actions):
            raise RuntimeError(f"{self.plugin_id}: manifest/adapter action mismatch: "
                               f"{sorted(declared ^ set(self._actions))}")

    def handlers(self) -> dict:
        raise NotImplementedError

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"

    # ---- lifecycle ------------------------------------------------------------------

    def configure(self, config: dict, secrets) -> None:
        """The client secret was already written to the shared slot by the
        runtime (`shared: true`); only the client id arrives as config."""
        self.connection.configure_client((config or {}).get("client_id"))

    def status(self) -> dict:
        return self.connection.plugin_status(self.plugin_id)

    # ---- actions ----------------------------------------------------------------------

    def perform(self, action: str, params: dict, scope: dict) -> Result:
        handler = self._actions.get(action)
        if handler is None:
            raise AdapterError(404, f"unknown action {action!r}")
        if not isinstance(params, dict):
            raise AdapterError(400, "params must be an object")
        return handler(params, CallScope(scope, self._forms))

    def now(self) -> float:
        return self._clock()

    def _kind(self, kind: str) -> None:
        if kind not in self.RESOURCE_KINDS:
            raise AdapterError(400, f"unknown resource kind {kind!r}")


# ---- param helpers shared by the adapters ---------------------------------------------

def text(params: dict, name: str, *, required: bool = False, default: str | None = None,
         strip: bool = True) -> str | None:
    value = params.get(name)
    if value is None:
        if required:
            raise AdapterError(400, f"{name} is required")
        return default
    if not isinstance(value, str):
        raise AdapterError(400, f"{name} must be a string")
    try:
        value.encode("utf-8")               # lone surrogates cannot be sent
    except UnicodeEncodeError:
        raise AdapterError(400, f"{name} is not valid text") from None
    if required and not value.strip():
        raise AdapterError(400, f"{name} must not be empty")
    return value.strip() if strip else value


def integer(params: dict, name: str, default: int, lo: int, hi: int) -> int:
    value = params.get(name, default)
    if value is None:
        value = default
    if isinstance(value, bool) or not isinstance(value, int):
        raise AdapterError(400, f"{name} must be an integer")
    return max(lo, min(value, hi))


def boolean(params: dict, name: str, default: bool = False) -> bool:
    value = params.get(name, default)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise AdapterError(400, f"{name} must be a boolean")
    return value


def strings(params: dict, name: str, limit: int = 100) -> list[str]:
    value = params.get(name)
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise AdapterError(400, f"{name} must be a list of strings")
    if len(value) > limit:
        raise AdapterError(400, f"{name} has more than {limit} entries")
    return value
