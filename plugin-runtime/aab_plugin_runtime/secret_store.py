"""The plugin's own encrypted secret store (Fernet, one file per slot).

Credentials live here, inside the plugin container, under the service's own
PLUGIN_SECRETS_KEY_<SERVICE>; the broker holds none of them. The store is
write-only from the network: `/configure` writes secret fields and nothing
in the plugin API ever reads them back. Adapters read them in-process
through a `SecretReader`.

Fail-closed rules:
  * boot refuses to start if the key is missing but encrypted files exist
    (a restart without the key must not look like "no credentials yet");
  * a key that no longer decrypts (rotated or wrong) raises
    `SecretsUnreadable`, which the runtime reports as "reconnect required"
    and maps to 503 on calls: nothing was performed;
  * reconnecting is a write: a slot that does not decrypt is replaced by the
    values written now (never merged, never read back), so re-entering the
    credentials in the console is the whole recovery.

Paths are plain files under a directory the service user owns, so the
container can run as a non-root user with only its own volume mounted.
"""

import json
import logging
import os
import re
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .errors import AdapterError
from .logging_setup import kv

_SLOT_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_SUFFIX = ".secrets"

log = logging.getLogger(__name__)


class SecretsUnreadable(Exception):
    """Encrypted data exists but this key cannot decrypt it."""


class SecretStore:
    def __init__(self, directory: str | Path, key: str | None):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not key:
            if self.has_content():
                raise RuntimeError(
                    f"secret store {self.dir} holds encrypted data but no secrets key "
                    "is configured (PLUGIN_SECRETS_KEY); refusing to start")
            self._fernet = None
        else:
            # An invalid key raises here, at boot, rather than on first use.
            self._fernet = Fernet(key.encode() if isinstance(key, str) else key)

    # ---- inspection --------------------------------------------------------

    def has_content(self) -> bool:
        return any(p.suffix == _SUFFIX for p in self.dir.iterdir() if p.is_file())

    def _path(self, slot: str) -> Path:
        # The slot becomes a file name: only id-shaped names, never a path.
        if not isinstance(slot, str) or not _SLOT_RE.match(slot):
            raise ValueError(f"invalid secret slot {slot!r}")
        return self.dir / f"{slot}{_SUFFIX}"

    # ---- reads (in-process only) ------------------------------------------

    def read_all(self, slot: str) -> dict[str, str]:
        path = self._path(slot)
        if not path.exists():
            return {}
        if self._fernet is None:
            raise SecretsUnreadable("no secrets key configured")
        try:
            return json.loads(self._fernet.decrypt(path.read_bytes()))
        except InvalidToken as exc:
            raise SecretsUnreadable("secrets do not decrypt with the current key; "
                                    "reconnect required") from exc

    def reader(self, slot: str) -> "StoreReader":
        return StoreReader(self, slot)

    # ---- writes ------------------------------------------------------------

    def write(self, slot: str, values: dict[str, str | None]) -> None:
        """Merge `values` into the slot. None or "" deletes that name.

        A slot that no longer decrypts (the key was rotated or replaced) is
        replaced instead of merged: under this key its contents are gone
        anyway, and refusing the write left "reconnect required" with no way
        to reconnect (every /configure, connect and disconnect answered 503).
        """
        if self._fernet is None:
            raise AdapterError(503, "secret store has no key (PLUGIN_SECRETS_KEY)")
        try:
            current = self.read_all(slot)
        except SecretsUnreadable:
            # The slot name only: never contents, old or new.
            log.warning("secret slot does not decrypt with the current key; replacing it "
                        "with the values being written %s", kv(slot=slot))
            current = {}
        before = set(current)
        for name, value in values.items():
            if not isinstance(name, str) or not name:
                raise AdapterError(400, "secret names must be non-empty strings")
            if value in (None, ""):
                current.pop(name, None)
            elif isinstance(value, str):
                current[name] = value
            else:
                raise AdapterError(400, f"secret {name!r} must be a string")
        self._atomic_write(self._path(slot), self._fernet.encrypt(
            json.dumps(current, sort_keys=True).encode()))
        # Slot and field names only, and only what changed: clearing a name
        # that was never set (a config form's empty field) is not news.
        stored = sorted(n for n, v in values.items() if v not in (None, ""))
        removed = sorted(n for n, v in values.items() if v in (None, "") and n in before)
        if stored or removed:
            log.info("secret slot written %s", kv(slot=slot, stored=stored, removed=removed))

    def wipe(self, slot: str) -> None:
        path = self._path(slot)
        if path.exists():
            path.unlink()
            log.info("secret slot wiped %s", kv(slot=slot))

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        # Write-then-rename so a crash never leaves a half-written ciphertext
        # (which would read as "key changed" and force a reconnect).
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise


class StoreReader:
    """The SecretReader handed to adapters: read-only, re-reads on each get so
    a later /configure is visible without re-wiring the adapter."""

    def __init__(self, store: SecretStore, slot: str):
        self._store, self._slot = store, slot

    def get(self, name: str, default: str | None = None) -> str | None:
        return self._store.read_all(self._slot).get(name, default)

    def __repr__(self) -> str:   # never show values in logs or tracebacks
        return f"StoreReader(slot={self._slot!r})"
