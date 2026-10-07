"""The installer's HTTP API, served to the broker only (net_installer, X-Installer-Token).

Routes (every one but /health requires `X-Installer-Token`, compared in
constant time; an empty configured token refuses to boot, exactly the
plugin runtime's X-Plugin-Token contract):

  GET  /health          liveness, tokenless: {"ok": true}
  POST /inspect         {source, ref, git_token?} -> descriptor, manifests and the resolved
                        commit (synchronous: clone to a temporary directory, read, delete)
  POST /install         {source, ref, commit, git_token?} -> 202 job
  POST /upgrade         {service, source, ref, commit, git_token?} -> 202 job
  POST /remove          {service, purge} -> 202 job
  GET  /jobs/{id}       the job: state queued|running|done|failed, log lines
  GET  /installed       {"items": [install records]}
  GET  /services        {"items": [{service, url, token}]}: how the broker reaches each
                        installed service. The token is PLUGIN_TOKEN_<SERVICE>, read from
                        .env at each request. The broker already holds every plugin token
                        (it is the caller each one authenticates); this route lets it learn
                        a new service's token without being recreated for a new
                        environment. Never logged, never in a job record.

`git_token` is the read-only GitHub token the owner stored in the broker's
console, sent only when one is stored. It is used for that request's clone
(inspect) or that job's single clone at its start (install, upgrade), given
to git through GIT_ASKPASS for github.com sources only (git.py), and kept
nowhere: not in the job record, install.json, a job log line or a log line
(the job's lines are masked for it whatever its shape). A malformed one is
a 400 that does not echo it.

Errors are `{"error": message, "code": code}` like the broker's: 400
bad_request, 401 unauthorized, 403 source_not_allowed, 404 not_found /
not_installed, 409 busy / source_changed, 422 invalid_package, 502
clone_failed, 503 unavailable. A job that fails later carries its reason in
the job's `error`, never in an HTTP status.

Logging (docs/logging.md): the shared logging_setup and request_log
(byte-identical copies, kept so by a broker test), as `installer`. Lines
carry sources, refs, commits, services and job ids; never INSTALLER_TOKEN,
never a plugin token and never a GitHub token (the ready line says
`git_auth=per_request`: the installer holds none of its own).
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
from .git import Git, GitError, check_token
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
    # The broker's GitHub token for this request's clone, when the owner stored
    # one (shape checked by git.check_token). Never in a repr.
    git_token: str | None = Field(default=None, max_length=255, repr=False)


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
    git = git or Git(askpass_dir=settings.state_dir)
    installer = Installer(settings, git, Compose(settings.home, runner), env_runner)
    # Job lines are shown to the owner: INSTALLER_TOKEN may never be one of
    # them, and neither may the GitHub token a job's request carried (masked
    # per job, see _submit).
    store = JobStore(settings.state_dir / "jobs", mask=(settings.token,))
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
        token = check_token(body.git_token)
        out = installer.inspect(body.source, body.ref, token)
        log.info("package inspected %s", kv(source=out["source"], ref=out["ref"],
                                            commit=out["commit"],
                                            service=out["descriptor"]["service"],
                                            plugins=out["descriptor"]["plugins"]))
        return out

    def _submit(kind: str, params: dict, work, token: str = "") -> JSONResponse:
        # The token rides with the work (in the closure) and as the job's
        # mask, never in `params`, which become the stored job record. Every
        # plugin token already in .env is masked too, whatever its shape (a
        # generated one is 64-hex and masked anyway; a hand-written one may
        # not be).
        try:
            job = store.submit(kind, params, work, mask=(token, *installer.known_tokens()))
        except Busy:
            raise InstallerError(409, "another install, upgrade or remove is in progress",
                                 "busy") from None
        return JSONResponse(public(job), status_code=202)

    @app.post("/install")
    def install(body: InstallBody) -> JSONResponse:
        token = check_token(body.git_token)
        source = installer.check_source(body.source, body.ref)
        commit = installer.check_commit(body.commit)
        ref = body.ref
        return _submit("install", {"source": source, "ref": ref, "commit": commit},
                       lambda ctx: installer.install(ctx, source, ref, commit, token), token)

    @app.post("/upgrade")
    def upgrade(body: UpgradeBody) -> JSONResponse:
        token = check_token(body.git_token)
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
                       lambda ctx: installer.upgrade(ctx, service, source, ref, commit, token),
                       token)

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

    @app.get("/services")
    def services() -> dict:
        # Answered to the broker only (the token guard above); the access
        # line has the path alone, and nothing here logs the items.
        return {"items": installer.services()}

    # git_auth: the installer holds no GitHub token; the broker sends one with
    # each request that needs it (named here, never shown anywhere).
    log.info("installer ready %s", kv(version=__version__, home=str(settings.home),
                                      allowed_sources=list(settings.allowed_sources) or None,
                                      git_auth="per_request"))
    return app
