"""Build the plugin-github app from the container's environment.

The container receives only its own values (docs/architecture.md 2.2); the
names are generic so the image does not care which .env variable fed them:

  PLUGIN_TOKEN        the broker's X-Plugin-Token for this service (required)
  PLUGIN_SECRETS_KEY  Fernet key for this service's secret store
  PLUGIN_SECRETS_DIR  where that store lives (the /secrets volume)

All three are read by `aab_plugin_runtime.from_env`, which refuses to boot
with an empty token. Everything GitHub-specific (App id, slug, key or key
file path, PAT) is console config (configuration principle), stored in the
encrypted store; no GitHub value comes from env.

GitHub rate limits come back as 429 with a `Retry-After` header: the
runtime's generic handler knows only `{"error"}` bodies, so this app adds a
more specific handler for `RateLimited` (Starlette picks the most specific
class in the exception's MRO).

Served as a factory (`uvicorn --factory aab_plugin_github.main:create_app`)
so importing this module reads no environment.
"""

import os
from collections.abc import Mapping

from aab_plugin_runtime import from_env
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .adapter import GitHubAdapter
from .api import RateLimited


def build_adapter(environ: Mapping[str, str]) -> GitHubAdapter:
    # `environ` is accepted for symmetry with the other plugins; nothing
    # GitHub-specific is read from it.
    return GitHubAdapter()


async def rate_limited(_: Request, exc: RateLimited) -> JSONResponse:
    return JSONResponse({"error": exc.message}, status_code=429,
                        headers={"Retry-After": str(exc.retry_after)})


def create_app(environ: Mapping[str, str] | None = None,
               adapter: GitHubAdapter | None = None) -> FastAPI:
    env = os.environ if environ is None else environ
    app = from_env([adapter or build_adapter(env)], dict(env))
    app.add_exception_handler(RateLimited, rate_limited)
    return app
