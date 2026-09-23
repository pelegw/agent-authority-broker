#!/usr/bin/env bash
# Run this ONCE on a fresh EC2 instance (Amazon Linux 2023 or Ubuntu) to install
# Docker + the Compose plugin and python3, add swap (so image builds don't OOM on small
# instances), and create the app directory. Re-running it is safe.
#
#   scp -i key.pem deploy/provision-ec2.sh ec2-user@<host>:~
#   ssh -i key.pem ec2-user@<host> 'bash provision-ec2.sh'
set -euo pipefail

APP_DIR=/opt/aab

echo "==> Detecting distro"
. /etc/os-release
echo "    $PRETTY_NAME"

install_docker_amzn() {
  sudo dnf -y update
  sudo dnf -y install docker
  # Compose v2 as a CLI plugin (dnf's docker on AL2023 ships without it).
  sudo mkdir -p /usr/libexec/docker/cli-plugins
  local ver="v2.29.7"
  sudo curl -fsSL \
    "https://github.com/docker/compose/releases/download/${ver}/docker-compose-linux-$(uname -m)" \
    -o /usr/libexec/docker/cli-plugins/docker-compose
  sudo chmod +x /usr/libexec/docker/cli-plugins/docker-compose
}

install_docker_debian() {
  sudo apt-get update
  sudo apt-get -y install ca-certificates curl gnupg
  sudo install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  sudo chmod a+r /etc/apt/keyrings/docker.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
  sudo apt-get update
  sudo apt-get -y install docker-ce docker-ce-cli containerd.io docker-compose-plugin
}

if ! command -v docker >/dev/null 2>&1; then
  echo "==> Installing Docker + Compose"
  case "${ID}" in
    amzn) install_docker_amzn ;;
    ubuntu|debian) install_docker_debian ;;
    *) echo "Unsupported distro '${ID}'. Install Docker + the compose plugin manually."; exit 1 ;;
  esac
else
  echo "==> Docker already present: $(docker --version)"
fi

# scripts/init_secrets.py (stdlib only) generates .env on the host, so the
# secrets are born where they are used and never cross the network.
if ! command -v python3 >/dev/null 2>&1; then
  echo "==> Installing python3 (for scripts/init_secrets.py)"
  case "${ID}" in
    amzn) sudo dnf -y install python3 ;;
    *) sudo apt-get -y install python3 ;;
  esac
fi

echo "==> Enabling and starting Docker"
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER" || true   # so you can run docker without sudo (re-login to apply)

if ! sudo swapon --show | grep -q .; then
  echo "==> Creating 2G swap (helps image builds on small instances)"
  sudo fallocate -l 2G /swapfile || sudo dd if=/dev/zero of=/swapfile bs=1M count=2048
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab > /dev/null
else
  echo "==> Swap already configured"
fi

echo "==> Creating ${APP_DIR}"
sudo mkdir -p "${APP_DIR}"
sudo chown "$USER":"$USER" "${APP_DIR}"

# Host-only directory bind-mounted read-only into plugin-github (and nothing
# else) at /run/secrets/github, for the optional GitHub App key file. Owned by
# uid 10001 (the containers' non-root user) and closed to everyone else;
# deploy/push.sh never syncs or deletes data/.
echo "==> Creating ${APP_DIR}/data/github-app (GitHub App key, plugin-github only)"
sudo mkdir -p "${APP_DIR}/data/github-app"
sudo chown 10001:10001 "${APP_DIR}/data/github-app"
sudo chmod 0700 "${APP_DIR}/data/github-app"

echo
echo "Done. Log out and back in (for the docker group) before deploying."
echo "Next: place edge/certs on the host, then run deploy/push.sh from your laptop."
echo "The first push generates .env on the host with scripts/init_secrets.py"
echo "(broker secrets plus a token and a key per plugin service)."
