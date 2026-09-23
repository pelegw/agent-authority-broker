#!/usr/bin/env python3
"""aab: admin CLI for the Agent Authority Broker.

Placeholder in phase 0: only `--version` works. Later phases add `aab setup`,
`aab decisions verify`, `aab skill build`, and the admin subcommands, all
talking to the same admin REST API as the console.
"""

import argparse

from broker import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aab", description=__doc__.splitlines()[0])
    parser.add_argument("--version", action="version", version=f"aab {__version__}")
    parser.parse_args(argv)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
