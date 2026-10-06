"""The installer's settings, read once from its environment (never from a request).

Everything that bounds the installer is environment-only on purpose: a
hijacked console session can reach the broker's admin API, and through it the
installer's routes, but never this process's environment. So the allowlist of
sources and the token cannot be widened or swapped over the network.

  INSTALLER_TOKEN            the X-Installer-Token the broker presents; empty refuses to boot
  INSTALLER_ALLOWED_SOURCES  comma-separated repositories it may clone, e.g.
                             `github.com/you/*`; empty refuses every inspect and install
  AAB_HOME                   the gateway checkout, mounted at the same path as on the
                             host (default /opt/aab): the compose project directory
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .git import parse_allowlist

DEFAULT_HOME = "/opt/aab"


@dataclass(frozen=True)
class Settings:
    token: str = field(repr=False)            # never in a repr, a log line or a traceback
    allowed_sources: tuple[str, ...]
    home: Path

    @classmethod
    def from_env(cls, environ: dict | None = None) -> "Settings":
        env = os.environ if environ is None else environ
        home = (env.get("AAB_HOME") or DEFAULT_HOME).strip()
        return cls(token=env.get("INSTALLER_TOKEN", ""),
                   allowed_sources=parse_allowlist(env.get("INSTALLER_ALLOWED_SOURCES", "")),
                   home=Path(home))

    def check(self) -> None:
        """Refuse to boot on an unsafe configuration."""
        if not isinstance(self.token, str) or not self.token.strip():
            raise RuntimeError("INSTALLER_TOKEN is empty; refusing to serve an open installer")
        if not self.home.is_absolute():
            # Compose resolves the overlays' relative paths against it, and the
            # host must see the same path: a relative one means neither.
            raise RuntimeError("AAB_HOME must be an absolute path")

    @property
    def plugins_dir(self) -> Path:
        return self.home / "plugins.d"

    @property
    def state_dir(self) -> Path:
        """Installer-only state (jobs, temporary clones). The leading `_`
        keeps it out of every `plugins.d/<service>` listing."""
        return self.plugins_dir / "_installer"
