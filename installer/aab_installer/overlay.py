"""Render an external plugin's compose overlay from its descriptor, through a fixed template.

A plugin repository cannot supply compose YAML: this module writes the
overlay `plugins.d/<service>/compose.yml` from the validated descriptor, so by
construction every external plugin gets exactly the shape the in-tree
plugins have (the invariants broker/tests/test_config_files.py enforces for
them, enforced here by construction and by installer/tests/test_overlay.py):

  * one compose service `plugin-<service>`, built from the repository at
    `./plugins.d/<service>/src` with the descriptor's Dockerfile;
  * exactly one network, `net_<service>`, shared with the broker only (the
    broker joins it in this same overlay): no plugin can reach another
    plugin, the installer, or the edge;
  * named volumes only: `<service>_secrets` at /secrets (the runtime's
    encrypted secret store) plus the descriptor's own `<service>_*`
    volumes. No bind mount, so nothing on the host is reachable;
  * no published port, no extra capability, no new privileges;
  * PLUGIN_TOKEN / PLUGIN_SECRETS_KEY from this service's own .env entries
    (PLUGIN_TOKEN_<SVC>, PLUGIN_SECRETS_KEY_<SVC>), PLUGIN_SECRETS_DIR, the
    descriptor's literal environment and its allowlisted passthrough;
  * the rotated json-file logging every service uses, restart unless-stopped;
  * the broker gains `net_<service>`, PLUGIN_URL_<SVC> and PLUGIN_TOKEN_<SVC>.

Beside it, render_newrelic() writes `plugins.d/<service>/newrelic.yml`: the
plugin's logging switched to the log shipper, and nothing else.
scripts/compose-files.sh loads it only when NEWRELIC_ENABLED=true, after
docker-compose.newrelic.yml, which cannot name an installed plugin itself (a
compose override may not mention a service no loaded file defines). It is
written on every install and upgrade whatever the setting, so turning New
Relic on or off never needs a re-render.

Relative paths in an overlay resolve against the project directory (the
gateway checkout), not against the overlay's own directory, which is why the
build context is spelled from the project root.
"""

from __future__ import annotations

from pathlib import PurePosixPath

import yaml

from .descriptor import PASSTHROUGH_DEFAULTS, Descriptor

PLUGIN_PORT = 8090          # what the plugin runtime image serves on (plugins/base)
PLUGINS_DIR = "plugins.d"
# docker-compose.yml's x-logging: at most 5 files of 10 MB per container.
LOGGING = {"driver": "json-file", "options": {"max-size": "10m", "max-file": "5"}}
# docker-compose.newrelic.yml's x-fluentd (broker/tests/test_config_files.py
# keeps the two identical): Docker's fluentd driver to the log shipper on the
# host's loopback, async, so a shipper outage never blocks the plugin.
FLUENTD_LOGGING = {"driver": "fluentd",
                   "options": {"fluentd-address": "127.0.0.1:24224", "fluentd-async": "true",
                               "tag": "aab.{{.Name}}"}}
NEWRELIC_FILE = "newrelic.yml"

HEADER = """\
# Rendered by aab-installer from plugins.d/{service}/src/aab-plugin.yaml.
# Do not edit: every install or upgrade rewrites it from the descriptor.
# The plugin gets one network (net_{service}, shared with the broker only),
# named volumes only, no ports, and only its own token and key.
"""

NEWRELIC_HEADER = """\
# Rendered by aab-installer for plugin-{service}. Do not edit.
# scripts/compose-files.sh loads it only when NEWRELIC_ENABLED=true, after
# docker-compose.newrelic.yml: the plugin's log lines then go to the log
# shipper like every other service's (docs/logging.md). Logging only.
"""


def names(service: str) -> dict[str, str]:
    """Every name the overlay derives from `service`, in one place."""
    upper = service.upper()
    return {"compose_service": f"plugin-{service}", "network": f"net_{service}",
            "secrets_volume": f"{service}_secrets", "token_env": f"PLUGIN_TOKEN_{upper}",
            "key_env": f"PLUGIN_SECRETS_KEY_{upper}", "url_env": f"PLUGIN_URL_{upper}"}


def _required(var: str) -> str:
    # `:?` makes compose refuse to start when the .env lacks the value,
    # instead of starting a plugin with an empty token.
    return f"${{{var}:?missing in .env: the installer adds it (scripts/init_secrets.py --rotate {var})}}"


def service_dir(service: str) -> str:
    """The project-relative directory the installer owns for `service`."""
    return f"{PLUGINS_DIR}/{service}"


def document(d: Descriptor, svc_dir: str | PurePosixPath) -> dict:
    """The overlay as data (what render() dumps)."""
    if str(PurePosixPath(svc_dir)) != service_dir(d.service):
        # The template only ever points at the service's own directory.
        raise ValueError(f"service_dir must be {service_dir(d.service)!r}")
    n = names(d.service)
    env: dict[str, str] = {
        "PLUGIN_TOKEN": _required(n["token_env"]),
        "PLUGIN_SECRETS_KEY": _required(n["key_env"]),
        "PLUGIN_SECRETS_DIR": "/secrets",
    }
    env.update(d.environment)                    # literals; validated free of "$"
    for key in d.env_passthrough:
        env[key] = f"${{{key}:-{PASSTHROUGH_DEFAULTS[key]}}}"
    volumes = [f"{n['secrets_volume']}:/secrets"]
    volumes += [f"{name}:{target}" for name, target in d.volumes.items()]
    plugin = {
        "build": {"context": f"./{service_dir(d.service)}/src",
                  "dockerfile": d.build.dockerfile},
        "restart": "unless-stopped",
        "logging": LOGGING,
        "networks": [n["network"]],
        "volumes": volumes,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "environment": env,
    }
    broker = {
        "networks": [n["network"]],
        "environment": {
            n["url_env"]: f"http://{n['compose_service']}:{PLUGIN_PORT}",
            n["token_env"]: _required(n["token_env"]),
        },
    }
    return {
        "services": {n["compose_service"]: plugin, "broker": broker},
        "networks": {n["network"]: {}},
        "volumes": {n["secrets_volume"]: {}, **{name: {} for name in d.volumes}},
    }


def render(d: Descriptor, svc_dir: str | PurePosixPath) -> str:
    """The overlay YAML for `d`, to be written to `<svc_dir>/compose.yml`."""
    body = yaml.safe_dump(document(d, svc_dir), sort_keys=False, default_flow_style=False,
                          width=1000, allow_unicode=False)
    return HEADER.format(service=d.service) + body


def newrelic_document(service: str) -> dict:
    """The New Relic logging override as data: one service, one key."""
    return {"services": {names(service)["compose_service"]: {"logging": FLUENTD_LOGGING}}}


def render_newrelic(d: Descriptor) -> str:
    """The YAML for `plugins.d/<service>/newrelic.yml`. Built from the
    validated service name alone: nothing from the repository reaches it."""
    body = yaml.safe_dump(newrelic_document(d.service), sort_keys=False,
                          default_flow_style=False, width=1000, allow_unicode=False)
    return NEWRELIC_HEADER.format(service=d.service) + body
