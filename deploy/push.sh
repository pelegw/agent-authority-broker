#!/usr/bin/env bash
# Deploy from your laptop to the EC2 host: sync the code (rsync when present,
# else the committed tree via git archive over SSH), then (re)build and start
# the public stack over SSH. Secrets (.env) and certs (edge/certs) are
# NOT synced, and neither is data/ (host-only files such as the GitHub App key
# that only plugin-github mounts) nor plugins.d/ (the external plugins the
# installer put there; it owns that directory). On the first push, .env is
# generated ON THE HOST by scripts/init_secrets.py, so the secrets never leave
# the machine that uses them (see deploy/DEPLOY.md).
#
#   HOST=ec2-user@1.2.3.4 SSH_KEY=~/keys/aab.pem deploy/push.sh
#
# Env vars:
#   HOST        (required)  user@host of the EC2 instance
#   SSH_KEY     (required)  path to the private key in OpenSSH format (a PuTTY
#                           .ppk must be converted first:
#                           puttygen key.ppk -O private-openssh -o key.pem)
#   REMOTE_DIR  (optional)  app dir on the host (default /opt/aab); must equal
#                           AAB_HOME in the host's .env (the installer mounts it
#                           at the same path)
set -euo pipefail

: "${HOST:?set HOST=user@host}"
: "${SSH_KEY:?set SSH_KEY=path/to/key.pem}"
REMOTE_DIR="${REMOTE_DIR:-/opt/aab}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> Syncing ${REPO_ROOT} -> ${HOST}:${REMOTE_DIR}"
if command -v rsync >/dev/null 2>&1; then
  # --delete keeps the host in sync, but the excludes protect host-only secrets,
  # state volumes, installed plugins and local build artifacts from being
  # touched or shipped. An excluded path is never deleted on the host either
  # (no --delete-excluded): without the /plugins.d/ exclude, --delete would
  # wipe every plugin the installer installed.
  rsync -az --delete \
    -e "ssh -i ${SSH_KEY}" \
    --exclude '.git' \
    --exclude '.env' \
    --exclude '*.db' --exclude '*.db-wal' --exclude '*.db-shm' \
    --exclude 'edge/certs/*' \
    --exclude '/data/' \
    --exclude '/plugins.d/' \
    --exclude '**/.venv' \
    --exclude '**/__pycache__' \
    --exclude '**/.pytest_cache' \
    --exclude '**/.hypothesis' \
    "${REPO_ROOT}/" "${HOST}:${REMOTE_DIR}/"
else
  # No rsync (Git Bash on Windows ships without it): ship the COMMITTED tree
  # with git archive over the same SSH session. This can never carry .env, a
  # venv or a database, because none of those are tracked. Host-only files
  # (.env, edge/certs, data/, and plugins.d/, which is git-ignored and so never
  # in the archive) are outside it and are left alone: tar only adds and
  # overwrites, it deletes nothing. Tracked files removed since the last
  # deploy stay on the host until the next rsync deploy, which is harmless
  # for compose builds.
  echo "    (rsync not found: shipping the committed tree with git archive)"
  ( cd "${REPO_ROOT}" && git archive --format=tar HEAD ) \
    | ssh -i "${SSH_KEY}" "${HOST}" "mkdir -p '${REMOTE_DIR}' && tar -x -C '${REMOTE_DIR}'"
fi

echo "==> Building and starting the public stack on the host"
ssh -i "${SSH_KEY}" "${HOST}" bash -s <<REMOTE
set -euo pipefail
cd "${REMOTE_DIR}"
if [ ! -f .env ]; then
  # First deploy: generate every secret here, on the host (the broker's plus a
  # token and a key per plugin service). The script prints only the path and a
  # checklist, never a secret value. Compose hands each container only its own.
  python3 scripts/init_secrets.py --out .env
  echo
  echo 'STOP: fill SITE_DOMAIN, CF_ACCESS_TEAM_DOMAIN, CF_ACCESS_AUD and set'
  echo 'CF_ACCESS_ENABLED=true in ${REMOTE_DIR}/.env, then re-run deploy/push.sh.'
  exit 1
fi
test -f edge/certs/origin.pem || { echo 'ERROR: edge/certs/origin.pem missing (see edge/certs/README.md).'; exit 1; }
# A .env from before the per-plugin-service split (or before the installer)
# lacks these and compose would refuse to start; --rotate appends a missing
# key (generated here, never printed).
for name in PLUGIN_TOKEN_WHATSAPP PLUGIN_SECRETS_KEY_WHATSAPP PLUGIN_TOKEN_GITHUB \
            PLUGIN_SECRETS_KEY_GITHUB PLUGIN_TOKEN_GOOGLE PLUGIN_SECRETS_KEY_GOOGLE \
            INSTALLER_TOKEN; do
  grep -q "^\${name}=." .env || python3 scripts/init_secrets.py --out .env --rotate "\${name}"
done
# AAB_HOME is a path, not a secret: the checkout's own, where the installer
# finds it (mounted at the same path inside).
if ! grep -q '^AAB_HOME=' .env; then
  printf '%s\n' '# Absolute path of this checkout on the host (the installer mounts it at the same path).' \
    'AAB_HOME=${REMOTE_DIR}' >> .env
fi
if [ "\$(sed -n 's/^AAB_HOME=//p' .env | tail -n 1 | tr -d '\r')" != '${REMOTE_DIR}' ]; then
  echo 'WARNING: AAB_HOME in .env is not ${REMOTE_DIR}: the plugin installer would mount the wrong checkout.'
fi
# The compose file set is never hand-listed: the base file, the public overlay
# (SITE_DOMAIN set), the installer overlay (INSTALLER_ENABLED=true) and every
# installed plugin's rendered overlay (plugins.d/<service>/compose.yml).
C="docker compose \$(sh scripts/compose-files.sh)"
echo "compose files: \$(sh scripts/compose-files.sh)"
\$C up -d --build
\$C ps
REMOTE

echo
echo "Deployed: edge, broker, plugin-whatsapp, whatsapp-sidecar, plugin-github, plugin-google,"
echo "plus aab-installer and every installed plugin when INSTALLER_ENABLED=true."
echo "Open https://\${SITE_DOMAIN}/admin (via Cloudflare Access) to create the owner account."
