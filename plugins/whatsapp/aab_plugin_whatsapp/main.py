"""Build the plugin-whatsapp app from the container's environment.

The container receives only its own values (docs/architecture.md 2.2); the
names are generic so the image does not care which .env variable fed them:

  PLUGIN_TOKEN        the broker's X-Plugin-Token for this service (required)
  PLUGIN_SECRETS_KEY  Fernet key for this service's secret store
  PLUGIN_SECRETS_DIR  where that store lives (the /secrets volume)
  SIDECAR_URL         the Go sidecar on wa_internal
  SIDECAR_TOKEN       the sidecar's X-Internal-Token (required)
  MESSAGES_DB         the sidecar's archive, mounted read-only

The first three are read by `aab_plugin_runtime.from_env`. An empty
PLUGIN_TOKEN or SIDECAR_TOKEN refuses to boot: an open plugin API, or a
plugin that can only ever get 401s from its sidecar, must fail loudly.

Served as a factory (`uvicorn --factory aab_plugin_whatsapp.main:create_app`)
so importing this module reads no environment.
"""

import os
from collections.abc import Mapping

from aab_plugin_runtime import from_env
from fastapi import FastAPI

from .adapter import WhatsAppAdapter
from .archive import Archive
from .sidecar_client import SidecarClient

DEFAULT_SIDECAR_URL = "http://whatsapp-sidecar:8081"
DEFAULT_MESSAGES_DB = "/data/messages.db"


def build_adapter(environ: Mapping[str, str]) -> WhatsAppAdapter:
    sidecar = SidecarClient(environ.get("SIDECAR_URL") or DEFAULT_SIDECAR_URL,
                            environ.get("SIDECAR_TOKEN", ""))
    archive = Archive(environ.get("MESSAGES_DB") or DEFAULT_MESSAGES_DB)
    return WhatsAppAdapter(sidecar, archive)


def create_app(environ: Mapping[str, str] | None = None) -> FastAPI:
    env = os.environ if environ is None else environ
    return from_env([build_adapter(env)], dict(env), service="whatsapp")
