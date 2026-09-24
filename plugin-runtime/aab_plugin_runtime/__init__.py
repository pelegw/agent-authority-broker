"""aab_plugin_runtime: hosts Agent Authority Broker plugin adapters.

A plugin container runs `serve([...adapters], token, secrets_dir, key)`
under uvicorn. The broker talks to it over the internal plugin API
(docs/plugin-api.md); credentials stay in this process, encrypted under the
service's own key.
"""

from .adapter import Connection, PluginAdapter, Result, SecretReader
from .app import SecretSlot, serve
from .env import from_env
from .errors import AdapterError
from .secret_store import SecretStore, SecretsUnreadable

__all__ = ["AdapterError", "Connection", "PluginAdapter", "Result", "SecretReader",
           "SecretSlot", "SecretStore", "SecretsUnreadable", "from_env", "serve"]
