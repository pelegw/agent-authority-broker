# plugin-whatsapp: the aab-plugin-runtime hosting the WhatsApp adapter.
# Serves the plugin API on :8090 (net_whatsapp only, never published), reads the
# sidecar's archive from the read-only wa_data mount, and talks to the Go
# sidecar over wa_internal.
#
# Build contexts (docker-compose.yml): the default context is this directory
# (./plugins/whatsapp); the named context "runtime" is ./plugin-runtime.
# Standalone: docker build --build-context runtime=../../plugin-runtime -t aab-plugin-whatsapp .
FROM python:3.12-slim
WORKDIR /srv

# Code is installed as root and stays root-owned: read-only to the running
# process. Both packages go in one pip command so the aab-plugin-runtime
# dependency resolves from the local copy (it is not on PyPI).
COPY --from=runtime pyproject.toml ./plugin-runtime/pyproject.toml
COPY --from=runtime aab_plugin_runtime ./plugin-runtime/aab_plugin_runtime
COPY pyproject.toml ./plugin-whatsapp/pyproject.toml
COPY aab_plugin_whatsapp ./plugin-whatsapp/aab_plugin_whatsapp
RUN pip install --no-cache-dir ./plugin-runtime ./plugin-whatsapp \
 && rm -rf ./plugin-runtime ./plugin-whatsapp

# Run unprivileged as aab (uid 10001, the same uid every image uses, so the
# sidecar-owned archive in wa_data is readable here; the session is in
# wa_session, which this container never mounts). /secrets is created and owned in
# the image so a fresh named secret volume mounted there inherits that
# ownership; only this plugin mounts it.
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
# One worker: nothing here needs two, and a single process keeps the secret
# store's writes serialized.
CMD ["uvicorn", "--factory", "aab_plugin_whatsapp.main:create_app", "--host", "0.0.0.0", "--port", "8090", "--workers", "1"]
