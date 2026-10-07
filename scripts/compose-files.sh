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
#   docker-compose.newrelic.yml       when NEWRELIC_ENABLED=true in .env (opt-in), then
#                                     the logging override of each service it cannot
#                                     name itself, after the file that defines it:
#     ops/newrelic/public.yml           the edge (with docker-compose.public.yml)
#     ops/newrelic/installer.yml        aab-installer (with docker-compose.installer.yml)
#     plugins.d/<service>/newrelic.yml  each installed plugin whose overlay has one
#                                       (the installer renders it; older installs lack it)
#
# The New Relic files come last: a later file overrides an earlier one, and
# they switch every service's logging to the log shipper.
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

public=""
installer=""
newrelic=""
if [ -n "$(env_value SITE_DOMAIN)" ]; then public=1; fi
if [ "$(env_value INSTALLER_ENABLED)" = "true" ]; then installer=1; fi
if [ "$(env_value NEWRELIC_ENABLED)" = "true" ]; then newrelic=1; fi

out="-f docker-compose.yml"
if [ -n "$public" ]; then
  out="$out -f docker-compose.public.yml"
fi
if [ -n "$installer" ]; then
  out="$out -f docker-compose.installer.yml"
fi
# Valid service names only (no space can hide in one), kept for the New
# Relic pass below.
services=""
for f in plugins.d/*/compose.yml; do
  [ -f "$f" ] || continue
  svc="${f#plugins.d/}"
  svc="${svc%/compose.yml}"
  case "$svc" in
    ""|[!a-z]*|*[!a-z0-9]*) continue ;;
  esac
  out="$out -f $f"
  services="$services $svc"
done
if [ -n "$newrelic" ]; then
  out="$out -f docker-compose.newrelic.yml"
  if [ -n "$public" ]; then
    out="$out -f ops/newrelic/public.yml"
  fi
  if [ -n "$installer" ]; then
    out="$out -f ops/newrelic/installer.yml"
  fi
  for svc in $services; do
    if [ -f "plugins.d/$svc/newrelic.yml" ]; then
      out="$out -f plugins.d/$svc/newrelic.yml"
    fi
  done
fi
printf '%s\n' "$out"
