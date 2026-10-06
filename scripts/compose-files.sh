#!/bin/sh
# Print the `docker compose -f ...` arguments for this deployment, so nobody
# hand-lists compose files again (deploy/push.sh, the installer and the docs
# all use it):
#
#   docker compose $(scripts/compose-files.sh) up -d
#   scripts/compose-files.sh /opt/aab        # another checkout
#
# The set, in order:
#   docker-compose.yml                always
#   docker-compose.public.yml         when SITE_DOMAIN is set in .env (the public deploy)
#   docker-compose.installer.yml      when INSTALLER_ENABLED=true in .env (opt-in)
#   plugins.d/<service>/compose.yml   every installed external plugin (installer-owned)
#
# Paths are printed relative to the checkout: run compose from it, or with
# --project-directory pointing at it. Only plugins.d entries whose directory
# name is a valid service name are listed, so a stray directory can never
# split into extra arguments.
set -eu

root="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$root"

# The value of KEY in .env, empty when unset. The last assignment wins, as
# in compose; a trailing CR (a file edited on Windows) is dropped.
env_value() {
  if [ -f .env ]; then
    sed -n "s/^$1=//p" .env | tail -n 1 | tr -d '\r'
  fi
}

out="-f docker-compose.yml"
if [ -n "$(env_value SITE_DOMAIN)" ]; then
  out="$out -f docker-compose.public.yml"
fi
if [ "$(env_value INSTALLER_ENABLED)" = "true" ]; then
  out="$out -f docker-compose.installer.yml"
fi
for f in plugins.d/*/compose.yml; do
  [ -f "$f" ] || continue
  svc="${f#plugins.d/}"
  svc="${svc%/compose.yml}"
  case "$svc" in
    ""|[!a-z]*|*[!a-z0-9]*) continue ;;
  esac
  out="$out -f $f"
done
printf '%s\n' "$out"
