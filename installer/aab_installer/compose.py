"""Running `docker compose` for the gateway project, with an injectable command runner.

The compose file set is never hand-listed here: `scripts/compose-files.sh`
prints it (docker-compose.yml, the public overlay when SITE_DOMAIN is set,
the installer overlay when INSTALLER_ENABLED=true, then every
plugins.d/<service>/compose.yml, and with NEWRELIC_ENABLED=true the New Relic
overlay and its logging overrides in ops/newrelic/ and
plugins.d/<service>/newrelic.yml), and deploy/push.sh and the docs use the
same script, so the host and the installer always agree on what the stack is.

Commands run with `--project-directory <AAB_HOME>`: the installer mounts the
checkout at the same path as on the host, so every relative path in every
file resolves to the same host path the daemon will mount or build from.

The runner is the test seam: tests pass a fake that records argv and answers
with canned results, so no Docker is needed. Output is returned to the job,
which keeps only a redacted tail of it.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

PROJECT = "aab"                 # docker-compose.yml's `name:`; prefixes volumes and networks
COMPOSE_FILES_SCRIPT = "scripts/compose-files.sh"
COMPOSE_FILE_RE = re.compile(r"^(?:docker-compose(?:\.[a-z]+)?\.yml"
                             r"|ops/newrelic/(?:public|installer)\.yml"
                             r"|plugins\.d/[a-z][a-z0-9]{1,31}/(?:compose|newrelic)\.yml)$")
TIMEOUT = 1800                  # an image build can take a while


@dataclass(frozen=True)
class Result:
    returncode: int
    output: str                 # stdout and stderr, interleaved


# (argv, cwd, timeout) -> Result
Runner = Callable[[Sequence[str], Path, float], Result]


class ComposeError(RuntimeError):
    """A compose step failed; the message names the step and exit code only."""


def subprocess_runner(argv: Sequence[str], cwd: Path, timeout: float) -> Result:
    try:
        r = subprocess.run(list(argv), cwd=cwd, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return Result(124, "timed out")
    except OSError as exc:
        return Result(127, f"cannot run {argv[0]}: {type(exc).__name__}")
    return Result(r.returncode, r.stdout or "")


def tail(output: str, lines: int = 15) -> list[str]:
    return [line for line in output.splitlines() if line.strip()][-lines:]


class Compose:
    def __init__(self, home: Path, runner: Runner | None = None):
        self.home = Path(home)
        self.run = runner or subprocess_runner

    def files(self) -> list[str]:
        """`-f a.yml -f b.yml ...` as printed by scripts/compose-files.sh,
        checked token by token: only the known compose files are accepted."""
        r = self.run(["sh", COMPOSE_FILES_SCRIPT, str(self.home)], self.home, 30)
        if r.returncode != 0:
            raise ComposeError(f"{COMPOSE_FILES_SCRIPT} failed (exit {r.returncode})")
        tokens = r.output.split()
        if not tokens or len(tokens) % 2 or tokens[1] != "docker-compose.yml":
            raise ComposeError(f"{COMPOSE_FILES_SCRIPT} printed an unexpected file list")
        for flag, name in zip(tokens[::2], tokens[1::2]):
            if flag != "-f" or not COMPOSE_FILE_RE.match(name):
                raise ComposeError(f"{COMPOSE_FILES_SCRIPT} printed an unexpected file list")
        return tokens

    def command(self, *args: str) -> list[str]:
        return ["docker", "compose", "--project-directory", str(self.home), *self.files(), *args]

    def compose(self, *args: str, timeout: float = TIMEOUT) -> tuple[list[str], Result]:
        argv = self.command(*args)
        return argv, self.run(argv, self.home, timeout)

    def docker(self, *args: str, timeout: float = 120) -> tuple[list[str], Result]:
        argv = ["docker", *args]
        return argv, self.run(argv, self.home, timeout)
