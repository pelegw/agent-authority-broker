# aab-plugin-base: the image every external plugin builds FROM.
# Published as ghcr.io/pelegw/aab-plugin-base:<gateway version> (and :latest)
# by .github/workflows/release.yml on a v* tag; CI builds it without pushing.
# docs/plugin-packaging.md is the plugin author's side of this contract:
#
#   * Python 3.12 (python:3.12-slim), WORKDIR /srv;
#   * aab-plugin-runtime from this gateway's plugin-runtime/ (the version of
#     this release), plus uvicorn, which serves every plugin;
#   * the unprivileged user aab, uid/gid 10001, the uid every image in the
#     stack uses;
#   * /secrets owned by aab, mode 0700, declared a VOLUME: the installer mounts
#     the service's <service>_secrets volume there, a fresh named volume
#     inherits this ownership, and the runtime keeps its encrypted secret
#     store in it (PLUGIN_SECRETS_DIR);
#   * a TCP liveness check on :8090 (every plugin API route requires
#     X-Plugin-Token, which a healthcheck must not hold), EXPOSE 8090;
#   * USER aab, and no CMD: the plugin names its own app factory.
#
# A plugin's Dockerfile is then: FROM this image, USER root, COPY and
# pip install its package, USER aab, CMD ["uvicorn", "--factory",
# "<its module>:create_app", ..., "--port", "8090", "--workers", "1",
# "--no-access-log"].
#
# Build contexts: the default context is this directory (./plugins/base); the
# named context "runtime" is ./plugin-runtime, as for the in-tree plugin
# images. From the repo root:
#   docker build --build-context runtime=plugin-runtime -t aab-plugin-base plugins/base
FROM python:3.12-slim
WORKDIR /srv

# Installed as root and left root-owned: read-only to the running process.
# uvicorn comes with it so a plugin's own pip install fetches only its own
# dependencies (and, for a private gateway repository, never needs to clone it).
COPY --from=runtime pyproject.toml ./plugin-runtime/pyproject.toml
COPY --from=runtime aab_plugin_runtime ./plugin-runtime/aab_plugin_runtime
RUN pip install --no-cache-dir ./plugin-runtime "uvicorn>=0.30,<1" \
 && rm -rf ./plugin-runtime

# Run unprivileged as aab (uid 10001, like every image). /secrets is created
# and owned in the image so a fresh named secret volume mounted there inherits
# that ownership; only this plugin's service mounts it.
RUN groupadd --system --gid 10001 aab \
 && useradd --system --uid 10001 --gid aab --no-create-home --shell /usr/sbin/nologin aab \
 && mkdir -p /secrets \
 && chown aab:aab /secrets \
 && chmod 0700 /secrets
USER aab
VOLUME /secrets
ENV PLUGIN_SECRETS_DIR=/secrets \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

EXPOSE 8090
# Every plugin API route, /manifests included, requires X-Plugin-Token, and
# the healthcheck must not hold that token. Liveness is therefore "the port
# accepts a TCP connection".
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import socket; socket.create_connection(('127.0.0.1', 8090), timeout=3).close()"]
# No CMD: each plugin sets its own (uvicorn --factory <module>:create_app ...).
