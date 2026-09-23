FROM python:3.12-slim
WORKDIR /srv
# VERSION lives at the repo root (single source), outside this build context;
# docker-compose.yml passes the repo root as the named context "repo".
# Standalone: docker build --build-context repo=.. -t aab-broker .
COPY --from=repo VERSION ./
COPY pyproject.toml ./
COPY broker ./broker
COPY cli ./cli
# hatch reads __version__ from broker/__init__.py, which walks up to /srv/VERSION.
RUN pip install --no-cache-dir .

# Run unprivileged. uid 10001 matches the WhatsApp sidecar so the shared
# wa_data volume (written by the sidecar, read-only here) is readable.
# /gwdata is created and owned in the image so a fresh named volume mounted
# there inherits that ownership. /srv stays root-owned: the code is read-only
# to the running process.
RUN groupadd --system --gid 10001 aab \
 && useradd --system --uid 10001 --gid aab --no-create-home --shell /usr/sbin/nologin aab \
 && mkdir -p /gwdata \
 && chown aab:aab /gwdata
USER aab

EXPOSE 8080
# python:slim has no curl; urllib is enough for a liveness probe.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4).status == 200 else 1)"]
# Single worker on purpose: in-process rate limiter + SQLite single-writer.
CMD ["uvicorn", "broker.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
