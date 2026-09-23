#!/usr/bin/env bash
# Deploy from your laptop to the EC2 host: sync the code, then (re)build and
# start the public stack over SSH. Secrets (.env) and certs (edge/certs) are
# NOT synced, and neither is data/ (host-only files such as the GitHub App key
# that only plugin-github mounts). On the first push, .env is generated ON THE HOST by
# scripts/init_secrets.py, so the secrets never leave the machine that uses
# them (see deploy/DEPLOY.md).
#
#   HOST=ec2-user@1.2.3.4 SSH_KEY=~/keys/aab.pem deploy/push.sh
#
# Env vars:
#   HOST        (required)  user@host of the EC2 instance
#   SSH_KEY     (required)  path to the .pem private key
#   REMOTE_DIR  (optional)  app dir on the host (default /opt/aab)
set -euo pipefail

: "${HOST:?set HOST=user@host}"
: "${SSH_KEY:?set SSH_KEY=path/to/key.pem}"
REMOTE_DIR="${REMOTE_DIR:-/opt/aab}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> Syncing ${REPO_ROOT} -> ${HOST}:${REMOTE_DIR}"
# --delete keeps the host in sync, but the excludes protect host-only secrets,
# state volumes, and local build artifacts from being touched or shipped.
rsync -az --delete \
  -e "ssh -i ${SSH_KEY}" \
  --exclude '.git' \
  --exclude '.env' \
  --exclude '*.db' --exclude '*.db-wal' --exclude '*.db-shm' \
  --exclude 'edge/certs/*' \
  --exclude '/data/' \
  --exclude '**/.venv' \
  --exclude '**/__pycache__' \
  --exclude '**/.pytest_cache' \
  --exclude '**/.hypothesis' \
  "${REPO_ROOT}/" "${HOST}:${REMOTE_DIR}/"

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
# A .env from before the per-plugin-service split lacks these and compose would
# refuse to start; --rotate appends a missing key (generated here, never printed).
for name in PLUGIN_TOKEN_WHATSAPP PLUGIN_SECRETS_KEY_WHATSAPP PLUGIN_TOKEN_GITHUB \
            PLUGIN_SECRETS_KEY_GITHUB PLUGIN_TOKEN_GOOGLE PLUGIN_SECRETS_KEY_GOOGLE; do
  grep -q "^\${name}=." .env || python3 scripts/init_secrets.py --out .env --rotate "\${name}"
done
docker compose -f docker-compose.yml -f docker-compose.public.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.public.yml ps
REMOTE

echo
echo "Deployed: edge, broker, plugin-whatsapp, whatsapp-sidecar, plugin-github, plugin-google."
echo "Open https://\${SITE_DOMAIN}/admin (via Cloudflare Access) to create the owner account."
