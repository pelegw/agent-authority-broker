"""What inspect, install, upgrade and remove do, step by step, and the record of what is installed.

The installer owns `plugins.d/` on the host (deploy/push.sh never syncs it):

    plugins.d/<service>/src/            the plugin repository at the reviewed commit
    plugins.d/<service>/compose.yml     the overlay rendered from its descriptor
    plugins.d/<service>/newrelic.yml    its logging override for the New Relic overlay
    plugins.d/<service>/install.json    source, ref, commit, plugins, volumes, when
    plugins.d/_installer/               jobs and temporary clones (never a service)

Install: fetch the reviewed commit into a temporary directory, read the
package, refuse a service that already exists, move the checkout into place,
make sure the service's two .env secrets exist, render the overlay, then
`up -d --build plugin-<service>` and `up -d broker` (the broker is recreated
with its new environment, without rebuilding it from whatever code the
checkout holds). Any failure rolls the files back, so a broken overlay can
never stay in the file set and break every later compose run, deploy
included. Upgrade is the same against an installed service (same source,
same service name) and restores the previous checkout and overlay on
failure. Remove stops and deletes the container, deletes the directory,
retires (or with purge deletes) the .env secrets, recreates the broker
without the service, and with purge deletes its volumes.

The authority decisions are the broker's: it pins the manifests the owner
reviewed before it asks for an install, and it registers a plugin only when
the service offers exactly those.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

from . import envfile
from .compose import PROJECT, Compose, ComposeError, tail
from .config import Settings
from .descriptor import RESERVED_SERVICES, SERVICE_RE, view
from .fs import rmtree, write_atomic
from .git import COMMIT_RE, Git, GitError, allowed, normalize_source, ref_kind
from .jobs import JobContext
from .overlay import NEWRELIC_FILE, names, render, render_newrelic, service_dir
from .package import read_package

INSTALL_RECORD = "install.json"
# The files _materialize writes; an upgrade restores exactly these.
RENDERED = ("compose.yml", NEWRELIC_FILE, INSTALL_RECORD)
RECORD_KEYS = ("service", "source", "ref", "commit", "plugins", "volumes", "installed_at",
               "updated_at")


class InstallerError(Exception):
    """A refusal with the HTTP status (and code) the API answers."""

    def __init__(self, status: int, message: str, code: str):
        super().__init__(message)
        self.status, self.message, self.code = status, message, code


def valid_service(service: object) -> str:
    if (not isinstance(service, str) or not SERVICE_RE.match(service)
            or service in RESERVED_SERVICES):
        raise InstallerError(400, "service must be an installed plugin service name",
                             "bad_request")
    return service


# The record readers are plain functions as well as Installer methods, for
# render_newrelic's command line: building an Installer clears the temporary
# clones of a job that may be running.

def read_record(plugins_dir: Path, service: str) -> dict | None:
    """`service`'s install record, or None when it is not installed."""
    path = plugins_dir / service / INSTALL_RECORD
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, NotADirectoryError, ValueError):
        return None
    return {k: data.get(k) for k in RECORD_KEYS} if isinstance(data, dict) else None


def installed_records(plugins_dir: Path) -> list[dict]:
    out = []
    if plugins_dir.is_dir():
        for d in sorted(plugins_dir.iterdir()):
            if d.is_dir() and SERVICE_RE.match(d.name):
                rec = read_record(plugins_dir, d.name)
                if rec:
                    out.append(rec)
    return out


class Installer:
    def __init__(self, settings: Settings, git: Git, compose: Compose,
                 env_runner: envfile.Runner | None = None):
        self.settings, self.git, self.compose = settings, git, compose
        self._env_runner = env_runner
        self.tmp_root = settings.state_dir / "tmp"
        # Clones a previous run left behind (it was stopped mid-job).
        rmtree(self.tmp_root)
        self.tmp_root.mkdir(parents=True, exist_ok=True)

    # ---- requests ----------------------------------------------------------------

    def check_source(self, source: object, ref: object) -> str:
        """The normalized source, after the allowlist and the ref rules."""
        src = normalize_source(source)
        ref_kind(ref)
        if not allowed(src, self.settings.allowed_sources):
            # Env-only and fail closed: an empty allowlist refuses everything.
            raise GitError(403, f"{src} is not in INSTALLER_ALLOWED_SOURCES",
                           "source_not_allowed")
        return src

    @staticmethod
    def check_commit(commit: object) -> str:
        if not isinstance(commit, str) or not COMMIT_RE.match(commit):
            raise InstallerError(400, "commit must be the 40-hex commit inspect returned",
                                 "bad_request")
        return commit

    # ---- the installed record ------------------------------------------------------

    def record(self, service: str) -> dict | None:
        return read_record(self.settings.plugins_dir, service)

    def installed(self) -> list[dict]:
        return installed_records(self.settings.plugins_dir)

    # ---- inspect (synchronous) -----------------------------------------------------

    def _tmp(self, prefix: str) -> Path:
        # Short names: a clone's pack paths are long already (MAX_PATH on a
        # Windows development machine).
        self.tmp_root.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(dir=self.tmp_root, prefix=prefix))

    def inspect(self, source: object, ref: object, token: str = "") -> dict:
        """`token`: the broker's GitHub token for this clone only ("" for none)."""
        src = self.check_source(source, ref)
        tmp = self._tmp("i-")
        try:
            commit = self.git.fetch(src, ref, tmp / "src", token=token)
            pkg = read_package(tmp / "src")
        finally:
            rmtree(tmp)
        return {"source": src, "ref": ref, "commit": commit,
                "descriptor": view(pkg.descriptor), "manifests": list(pkg.manifests),
                "installed": self.record(pkg.descriptor.service)}

    # ---- job steps -------------------------------------------------------------------

    def _step(self, ctx: JobContext, argv: list[str], result, must: bool = True,
              label: str = "") -> bool:
        ctx.log("$ " + " ".join(argv))
        for line in tail(result.output):
            ctx.log("  " + line)
        ctx.log(f"exit {result.returncode}")
        if result.returncode != 0 and must:
            raise ComposeError(f"{label or argv[0]} failed (exit {result.returncode})")
        return result.returncode == 0

    def _up(self, ctx: JobContext, service: str) -> None:
        n = names(service)
        self._step(ctx, *self.compose.compose("up", "-d", "--build", n["compose_service"]),
                   label=f"building and starting {n['compose_service']}")
        # Recreated for its new environment and network; never rebuilt here.
        self._step(ctx, *self.compose.compose("up", "-d", "broker"),
                   label="recreating the broker")

    def _fetch(self, ctx: JobContext, tmp: Path, source: str, ref: str, commit: str,
               token: str = ""):
        # The job's one network fetch, at its start: the token is needed here
        # and nowhere after, so nothing keeps it beyond this job.
        ctx.log(f"fetching {source} at {ref}")
        got = self.git.fetch(source, ref, tmp / "src", token=token)
        ctx.log(f"resolved commit {got}")
        if got != commit:
            raise InstallerError(409, f"{ref} is now commit {got}, not the reviewed {commit}; "
                                      "inspect it again", "commit_changed")
        pkg = read_package(tmp / "src")
        ctx.log(f"package: service {pkg.descriptor.service}, plugins "
                f"{', '.join(pkg.descriptor.plugins)}")
        return pkg

    def _materialize(self, ctx: JobContext, pkg, source: str, ref: str, commit: str,
                     installed_at: int | None = None) -> None:
        d = pkg.descriptor
        svc_dir = self.settings.plugins_dir / d.service
        added = envfile.ensure(self.settings.home, d.service, runner=self._env_runner)
        ctx.log(".env: added " + ", ".join(added) if added else ".env: secrets already present")
        write_atomic(svc_dir / "compose.yml", render(d, service_dir(d.service)).encode("utf-8"))
        ctx.log(f"rendered {service_dir(d.service)}/compose.yml")
        # Always written; compose-files.sh decides whether it is loaded.
        write_atomic(svc_dir / NEWRELIC_FILE, render_newrelic(d.service).encode("utf-8"))
        now = int(time.time())
        record = {"service": d.service, "source": source, "ref": ref, "commit": commit,
                  "plugins": list(d.plugins),
                  "volumes": [names(d.service)["secrets_volume"], *d.volumes],
                  "installed_at": installed_at or now, "updated_at": now,
                  "descriptor": view(d)}
        write_atomic(svc_dir / INSTALL_RECORD,
                     json.dumps(record, indent=1, sort_keys=True).encode("utf-8"))

    def _best_effort(self, ctx: JobContext, *compose_args: str) -> None:
        try:
            self._step(ctx, *self.compose.compose(*compose_args), must=False)
        except ComposeError as exc:                  # the file list itself failed
            ctx.log(f"skipped: {exc}")

    def install(self, ctx: JobContext, source: str, ref: str, commit: str,
                token: str = "") -> None:
        tmp = self._tmp("j-")
        try:
            pkg = self._fetch(ctx, tmp, source, ref, commit, token)
            service = pkg.descriptor.service
            ctx.update(service=service)
            svc_dir = self.settings.plugins_dir / service
            if svc_dir.exists():
                raise InstallerError(409, f"{service} is already installed; upgrade it instead",
                                     "already_installed")
            svc_dir.mkdir(parents=True)
            os.replace(tmp / "src", svc_dir / "src")
            try:
                self._materialize(ctx, pkg, source, ref, commit)
                self._up(ctx, service)
            except BaseException:
                ctx.log("install failed; rolling back")
                if (svc_dir / "compose.yml").exists():
                    self._best_effort(ctx, "rm", "-s", "-f", names(service)["compose_service"])
                rmtree(svc_dir)
                self._best_effort(ctx, "up", "-d", "broker")
                raise
            ctx.log(f"installed {service} from {source}@{ref}")
        finally:
            rmtree(tmp)

    def upgrade(self, ctx: JobContext, service: str, source: str, ref: str, commit: str,
                token: str = "") -> None:
        previous = self.record(service)
        if previous is None:
            raise InstallerError(404, f"{service} is not installed", "not_installed")
        svc_dir = self.settings.plugins_dir / service
        tmp = self._tmp("j-")
        try:
            pkg = self._fetch(ctx, tmp, source, ref, commit, token)
            if pkg.descriptor.service != service:
                raise InstallerError(409, f"the package at {ref} is service "
                                          f"{pkg.descriptor.service}, not {service}",
                                     "service_changed")
            saved = {name: (svc_dir / name).read_bytes()
                     for name in RENDERED if (svc_dir / name).exists()}
            os.replace(svc_dir / "src", tmp / "previous-src")
            os.replace(tmp / "src", svc_dir / "src")
            try:
                self._materialize(ctx, pkg, source, ref, commit, previous.get("installed_at"))
                self._up(ctx, service)
            except BaseException:
                ctx.log("upgrade failed; restoring the previous version's files")
                rmtree(svc_dir / "src")
                os.replace(tmp / "previous-src", svc_dir / "src")
                for name in RENDERED:
                    if name in saved:
                        (svc_dir / name).write_bytes(saved[name])
                    else:
                        # New in this upgrade (newrelic.yml for an install
                        # made before it existed): gone again, as before.
                        (svc_dir / name).unlink(missing_ok=True)
                self._best_effort(ctx, "up", "-d", names(service)["compose_service"], "broker")
                raise
            ctx.log(f"upgraded {service} to {source}@{ref}")
        finally:
            rmtree(tmp)

    def remove(self, ctx: JobContext, service: str, purge: bool) -> None:
        rec = self.record(service)
        if rec is None:
            raise InstallerError(404, f"{service} is not installed", "not_installed")
        n = names(service)
        # Stopped while its overlay is still in the file set, so compose knows it.
        self._step(ctx, *self.compose.compose("rm", "-s", "-f", n["compose_service"]),
                   label=f"removing {n['compose_service']}")
        rmtree(self.settings.plugins_dir / service)
        ctx.log(f"deleted {service_dir(service)}")
        env = self.settings.home / ".env"
        if purge:
            gone = envfile.purge(env, service)
            ctx.log(".env: deleted " + (", ".join(gone) or "nothing"))
        else:
            kept = envfile.retire(env, service)
            ctx.log(".env: retired (commented, kept) " + (", ".join(kept) or "nothing"))
        self._step(ctx, *self.compose.compose("up", "-d", "broker"),
                   label="recreating the broker")
        self._step(ctx, *self.compose.docker("network", "rm", f"{PROJECT}_{n['network']}"),
                   must=False)
        if purge:
            for volume in rec.get("volumes") or []:
                if isinstance(volume, str) and volume.startswith(f"{service}_"):
                    self._step(ctx, *self.compose.docker("volume", "rm", f"{PROJECT}_{volume}"),
                               must=False)
        ctx.log(f"removed {service}" + (" and its volumes" if purge else "; volumes kept"))
