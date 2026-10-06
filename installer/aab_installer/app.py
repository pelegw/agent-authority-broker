"""The installer's HTTP API, served to the broker only (net_installer, X-Installer-Token).

Routes (every one but /health requires `X-Installer-Token`, compared in
constant time; an empty configured token refuses to boot, exactly the
plugin runtime's X-Plugin-Token contract):

  GET  /health          liveness, tokenless: {"ok": true}
  POST /inspect         {source, ref} -> descriptor, manifests and the resolved commit
                        (synchronous: clone to a temporary directory, read, delete)
  POST /install         {source, ref, commit} -> 202 job
  POST /upgrade         {service, source, ref, commit} -> 202 job
  POST /remove          {service, purge} -> 202 job
  GET  /jobs/{id}       the job: state queued|running|done|failed, log lines
  GET  /installed       {"items": [install records]}

Errors are `{"error": message, "code": code}` like the broker's: 400
bad_request, 401 unauthorized, 403 source_not_allowed, 404 not_found /
not_installed, 409 busy / source_changed, 422 invalid_package, 502
clone_failed, 503 unavailable. A job that fails later carries its reason in
the job's `error`, never in an HTTP status.

Logging (docs/logging.md): the shared logging_setup and request_log
(byte-identical copies, kept so by a broker test), as `installer`. Lines
carry sources, refs, commits, services and job ids; never the token, and
never INSTALLER_GIT_TOKEN (the ready line says only whether github.com
clones are authenticated).
"""

from __future__ import annotations

import hmac
import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from . import __version__, logging_setup
from .compose import Compose, Runner
from .config import Settings
from .envfile import Runner as EnvRunner
from .git import Git, GitError
from .jobs import Busy, JobStore, public
from .logging_setup import kv, set_actor
from .operations import Installer, InstallerError, valid_service
from .package import PackageError
from .request_log import RequestContextMiddleware, client_ip

log = logging.getLogger("aab_installer")

TOKEN_HEADER = "x-installer-token"
TOKENLESS = frozenset({"/health"})


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InspectBody(_Body):
    source: str = Field(max_length=300)
    ref: str = Field(max_length=64)


class InstallBody(InspectBody):
    commit: str = Field(max_length=64)


class UpgradeBody(InstallBody):
    service: str = Field(max_length=64)


class RemoveBody(_Body):
    service: str = Field(max_length=64)
    purge: bool = False


def _error(status: int, message: str, code: str) -> JSONResponse:
    return JSONResponse({"error": message, "code": code}, status_code=status)


def create_app(settings: Settings | None = None, *, git: Git | None = None,
               runner: Runner | None = None, env_runner: EnvRunner | None = None) -> FastAPI:
    """Build the installer app. Raises at boot on any unsafe configuration.
    `git`, `runner` and `env_runner` are the test seams."""
    logging_setup.configure("installer")
    settings = settings or Settings.from_env()
    settings.check()
    git = git or Git(token=settings.git_token, askpass_dir=settings.state_dir)
    installer = Installer(settings, git, Compose(settings.home, runner), env_runner)
    # Job lines are shown to the owner: neither token may ever be one of them.
    store = JobStore(settings.state_dir / "jobs", mask=(settings.token, settings.git_token))
    expected = settings.token.encode()

    app = FastAPI(title="aab installer", version=__version__, docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.state.store, app.state.installer = store, installer      # tests only

    @app.middleware("http")
    async def _auth(request: Request, call_next):
        if request.url.path not in TOKENLESS:
            supplied = request.headers.get(TOKEN_HEADER, "").encode()
            # compare_digest: the time taken does not leak how much of a
            # guessed token was right.
            if not hmac.compare_digest(supplied, expected):
                log.warning("installer call refused: bad or missing X-Installer-Token %s",
                            kv(path=request.url.path, ip=client_ip(request.scope),
                               header_present=bool(supplied)))
                return _error(401, "unauthorized", "unauthorized")
            set_actor("broker")
        try:
            return await call_next(request)
        except Exception as exc:
            # Mapped inside the request context so the line carries its id;
            # only the type is logged (a message could carry a path or worse).
            log.error("unexpected installer failure %s", kv(error=type(exc).__name__))
            return _error(500, "internal installer error", "internal")

    app.add_middleware(RequestContextMiddleware, access_logger="aab_installer.access")

    @app.exception_handler(GitError)
    async def _git_error(_: Request, exc: GitError):
        return _error(exc.status, exc.message, exc.code)

    @app.exception_handler(InstallerError)
    async def _installer_error(_: Request, exc: InstallerError):
        return _error(exc.status, exc.message, exc.code)

    @app.exception_handler(PackageError)
    async def _package_error(_: Request, exc: PackageError):
        return _error(422, f"not a valid plugin package: {exc}", "invalid_package")

    @app.exception_handler(RequestValidationError)
    async def _bad_body(_: Request, exc: RequestValidationError):
        return _error(400, "invalid request body", "bad_request")

    @app.api_route("/health", methods=["GET", "HEAD"])
    def health() -> dict:
        return {"ok": True}

    @app.post("/inspect")
    def inspect(body: InspectBody) -> dict:
        out = installer.inspect(body.source, body.ref)
        log.info("package inspected %s", kv(source=out["source"], ref=out["ref"],
                                            commit=out["commit"],
                                            service=out["descriptor"]["service"],
                                            plugins=out["descriptor"]["plugins"]))
        return out

    def _submit(kind: str, params: dict, work) -> JSONResponse:
        try:
            job = store.submit(kind, params, work)
        except Busy:
            raise InstallerError(409, "another install, upgrade or remove is in progress",
                                 "busy") from None
        return JSONResponse(public(job), status_code=202)

    @app.post("/install")
    def install(body: InstallBody) -> JSONResponse:
        source = installer.check_source(body.source, body.ref)
        commit = installer.check_commit(body.commit)
        ref = body.ref
        return _submit("install", {"source": source, "ref": ref, "commit": commit},
                       lambda ctx: installer.install(ctx, source, ref, commit))

    @app.post("/upgrade")
    def upgrade(body: UpgradeBody) -> JSONResponse:
        service = valid_service(body.service)
        source = installer.check_source(body.source, body.ref)
        commit = installer.check_commit(body.commit)
        current = installer.record(service)
        if current is None:
            raise InstallerError(404, f"{service} is not installed", "not_installed")
        if current.get("source") != source:
            # Another repository under the same service name is a remove and
            # an install, each reviewed on its own.
            raise InstallerError(409, f"{service} is installed from {current.get('source')}; "
                                      "remove it to install from another source",
                                 "source_changed")
        ref = body.ref
        return _submit("upgrade", {"service": service, "source": source, "ref": ref,
                                   "commit": commit},
                       lambda ctx: installer.upgrade(ctx, service, source, ref, commit))

    @app.post("/remove")
    def remove(body: RemoveBody) -> JSONResponse:
        service = valid_service(body.service)
        current = installer.record(service)
        if current is None:
            raise InstallerError(404, f"{service} is not installed", "not_installed")
        purge = body.purge
        return _submit("remove", {"service": service, "source": current.get("source"),
                                  "ref": current.get("ref"), "commit": current.get("commit"),
                                  "purge": purge},
                       lambda ctx: installer.remove(ctx, service, purge))

    @app.get("/jobs/{job_id}")
    def job(job_id: str) -> dict:
        found = store.get(job_id)
        if found is None:
            raise InstallerError(404, "no such job", "not_found")
        return public(found)

    @app.get("/installed")
    def installed() -> dict:
        return {"items": installer.installed()}

    log.info("installer ready %s", kv(version=__version__, home=str(settings.home),
                                      allowed_sources=list(settings.allowed_sources) or None,
                                      git_auth="askpass" if getattr(git, "authenticated", False)
                                      else "anonymous"))
    return app
