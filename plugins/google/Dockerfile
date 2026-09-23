# plugin-google: PLACEHOLDER image. Phase 7 fills it in: install the
# aab-plugin-runtime package (named build context "runtime" = ./plugin-runtime)
# plus the gmail, gcal and gdrive adapters, _google/client.py and the
# google_oauth connection,
# then serve the plugin API on :8090 (broker_net only, never published).
# The named context "repo" is the repo root (VERSION, vendored manifests).
#
# Until then the container only idles, so the compose topology (networks,
# volumes, env split) can be brought up and inspected end to end.
FROM python:3.12-slim
WORKDIR /srv

# Run unprivileged as aab (uid 10001, the same uid every image uses so a
# shared volume such as wa_data is readable). /secrets is created and owned in
# the image so a fresh named secret volume mounted there inherits that
# ownership; only this plugin mounts it. /srv stays root-owned (read-only code).
RUN groupadd --system --gid 10001 aab \
 && useradd --system --uid 10001 --gid aab --no-create-home --shell /usr/sbin/nologin aab \
 && mkdir -p /secrets \
 && chown aab:aab /secrets \
 && chmod 0700 /secrets
USER aab
VOLUME /secrets

EXPOSE 8090
CMD ["sleep", "infinity"]
