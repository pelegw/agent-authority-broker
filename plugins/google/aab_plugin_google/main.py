"""Build the plugin-google app from the container's environment.

The container receives only its own values (docs/architecture.md 2.2); the
names are generic, read by `aab_plugin_runtime.from_env`:

  PLUGIN_TOKEN        the broker's X-Plugin-Token for this service (required)
  PLUGIN_SECRETS_KEY  Fernet key for this service's secret store
  PLUGIN_SECRETS_DIR  where that store lives (the /secrets volume)

Nothing Google-specific comes from the environment: the OAuth client id and
secret are console configuration (relayed once through /configure), and the
redirect URI arrives from the broker with each /connect/start.

All three adapters share ONE connection object (one refresh token, one
token cache) and one API client. Served as a factory
(`uvicorn --factory aab_plugin_google.main:create_app`) so importing this
module reads no environment.
"""

import os
from collections.abc import Mapping

import httpx
from aab_plugin_runtime import from_env
from fastapi import FastAPI

from .adapters import GcalAdapter, GdriveAdapter, GmailAdapter
from .client import GoogleClient
from .connection import GoogleOAuthConnection


def build_adapters(transport: httpx.BaseTransport | None = None,
                   clock=None) -> list:
    """The three adapters over one connection. `transport` and `clock` are
    for tests (the fake Google and a fixed time)."""
    kw = {} if clock is None else {"clock": clock}
    connection = GoogleOAuthConnection(transport=transport, **kw)
    client = GoogleClient(connection, transport=transport)
    return [GmailAdapter(connection, client, **kw), GcalAdapter(connection, client, **kw),
            GdriveAdapter(connection, client, **kw)]


def create_app(environ: Mapping[str, str] | None = None) -> FastAPI:
    env = os.environ if environ is None else environ
    return from_env(build_adapters(), dict(env), service="google")
