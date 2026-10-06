# aab-installer: installs external plugins (docker-compose.installer.yml, opt-in).
# Serves its API on :8070 to the broker only (net_installer, never published),
# clones allowlisted plugin repositories with git and drives the host's
# Docker daemon through the mounted socket with the docker CLI and the
# compose plugin.
#
# Build contexts (docker-compose.installer.yml): the default context is this
# directory (./installer); the named context "repo" is the repo root, for the
# single VERSION file. Standalone: docker build --build-context repo=.. -t aab-installer .
FROM python:3.12-slim

# git for clones; docker-ce-cli, the compose and buildx plugins from Docker's
# own Debian repository (the distribution's docker.io would pull in a daemon).
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git \
 && install -m 0755 -d /etc/apt/keyrings \
 && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
 && chmod a+r /etc/apt/keyrings/docker.asc \
 && . /etc/os-release \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian ${VERSION_CODENAME} stable" \
      > /etc/apt/sources.list.d/docker.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends docker-ce-cli docker-compose-plugin docker-buildx-plugin \
 && apt-get purge -y curl \
 && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
# VERSION lives at the repo root (single source), outside this build context.
COPY --from=repo VERSION ./
COPY pyproject.toml ./
COPY aab_installer ./aab_installer
# hatch reads __version__ from aab_installer/__init__.py, which walks up to /srv/VERSION.
RUN pip install --no-cache-dir .

# Runs as root, deliberately: the Docker socket is root on the host whoever
# holds it, so an unprivileged user here would add no boundary, only break
# writes to the root-owned checkout (plugins.d, .env). What bounds this
# container is the network (the broker alone), INSTALLER_TOKEN, the
# env-only source allowlist, the overlays it renders and the commits it pins.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

EXPOSE 8070
# /health is the one tokenless route (liveness only).
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8070/health', timeout=4).status == 200 else 1)"]
# One worker: jobs run on one thread in one process, one at a time.
# --no-access-log: the installer writes its own access line (docs/logging.md).
CMD ["uvicorn", "--factory", "aab_installer.app:create_app", "--host", "0.0.0.0", "--port", "8070", "--workers", "1", "--no-access-log"]
