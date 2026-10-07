"""Write plugins.d/<service>/newrelic.yml for a plugin installed before the installer rendered it.

The installer writes that file on every install and upgrade (operations.py).
A plugin installed by an earlier version has none, so with
NEWRELIC_ENABLED=true its logs stay on the server until its next upgrade.
This command writes the file now, through the same function the installer
uses (overlay.render_newrelic), so the next upgrade rewrites it byte for
byte. Run it once, inside the installer container, where the checkout is
mounted at AAB_HOME:

    docker compose $(scripts/compose-files.sh) exec aab-installer \\
        python -m aab_installer.render_newrelic <service> [<service> ...]
    docker compose $(scripts/compose-files.sh) exec aab-installer \\
        python -m aab_installer.render_newrelic --all

It writes into the directory of an installed service only (one with an
install record), and nothing but that one file. Then run compose with the
file set printed again (it now names the new files): `up -d` applies them.
"""

from __future__ import annotations

import argparse
import sys

from .config import Settings
from .fs import write_atomic
from .operations import InstallerError, installed_records, read_record, valid_service
from .overlay import NEWRELIC_FILE, render_newrelic, service_dir


def write(settings: Settings, service: str) -> str:
    """Render and write one service's override; returns its relative path.
    Refuses a name that is not a service name (no path can be spelled with
    one) and a service that is not installed."""
    service = valid_service(service)
    if read_record(settings.plugins_dir, service) is None:
        raise InstallerError(404, f"{service} is not installed", "not_installed")
    path = settings.plugins_dir / service / NEWRELIC_FILE
    write_atomic(path, render_newrelic(service).encode("utf-8"))
    return f"{service_dir(service)}/{NEWRELIC_FILE}"


def main(argv: list[str] | None = None, environ: dict | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m aab_installer.render_newrelic",
        description="Write plugins.d/<service>/newrelic.yml (the New Relic logging "
                    "override) for installed plugins that lack it.")
    parser.add_argument("services", nargs="*", metavar="service",
                        help="an installed plugin service, for example finance")
    parser.add_argument("--all", action="store_true", help="every installed plugin service")
    args = parser.parse_args(argv)
    if bool(args.services) == args.all:
        parser.error("name one or more services, or pass --all")
    settings = Settings.from_env(environ)
    if not settings.home.is_absolute():
        print("AAB_HOME must be an absolute path", file=sys.stderr)
        return 2
    services = ([r["service"] for r in installed_records(settings.plugins_dir)]
                if args.all else args.services)
    if not services:
        print("no installed plugin services", file=sys.stderr)
        return 0
    status = 0
    for service in services:
        try:
            print(f"wrote {write(settings, service)}")
        except InstallerError as exc:
            print(f"{service}: {exc.message}", file=sys.stderr)
            status = 2
    return status


if __name__ == "__main__":
    raise SystemExit(main())
