"""`from_env()`: build the app from a container's environment.

Each plugin service receives only its own values (docs/architecture.md 2.2):
PLUGIN_TOKEN (the broker's shared token for this service), PLUGIN_SECRETS_KEY
(Fernet key for this service's store) and PLUGIN_SECRETS_DIR. The default
directory is under the running user's home, so a non-root container user
can write it without any root-owned path being prepared.

LOG_LEVEL and LOG_FORMAT are read by `serve()` (logging_setup.configure),
which names the process `plugin-<service>` in every log line.
"""

import os
from pathlib import Path

from fastapi import FastAPI

from .adapter import PluginAdapter
from .app import serve


def default_secrets_dir() -> Path:
    return Path.home() / ".aab-plugin" / "secrets"


def from_env(adapters: list[PluginAdapter], environ: dict | None = None, *,
             service: str | None = None) -> FastAPI:
    env = os.environ if environ is None else environ
    return serve(adapters,
                 token=env.get("PLUGIN_TOKEN", ""),
                 secrets_dir=env.get("PLUGIN_SECRETS_DIR") or default_secrets_dir(),
                 secrets_key=env.get("PLUGIN_SECRETS_KEY") or None,
                 service=service)
