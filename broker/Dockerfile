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
EXPOSE 8080
# Single worker on purpose: in-process rate limiter + SQLite single-writer.
CMD ["uvicorn", "broker.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
