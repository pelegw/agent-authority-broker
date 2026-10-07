"""Installer test fixtures: the example descriptor from the plan, a throwaway
project root shaped like /opt/aab (the real `scripts/init_secrets.py` and a
generated `.env`), the broker's echo plugin packaged as an external plugin
repository (a local bare git repository with release tags), a fake Docker
that records every command, and the installer app wired to all three.

Nothing here needs Docker or the network: compose runs through the injected
runner and git clones over file:// from the bare repository.
"""

import os
import shutil
import subprocess
import sys
import threading
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aab_installer import logging_setup

# Configured once, at collection, as the installer process does at boot:
# create_app() inside a test then leaves the handlers alone.
logging_setup.configure("installer-test")

REPO = Path(__file__).resolve().parents[2]
ECHO_MANIFEST = REPO / "broker" / "tests" / "fixtures" / "echo" / "manifest.yaml"
TOKEN = "installer-test-token-0123456789abcdef"
SOURCE = "github.com/acme/aab-plugin-echo"

# The descriptor the plan documents (and the finance plugin will ship).
FINANCE = """\
schema: 1
service: finance
plugins: [finance]
manifests: [aab_plugin_finance/manifest.yaml]
runtime: "0.3"
build: {dockerfile: Dockerfile}
volumes: {finance_data: /data}
environment: {FINANCE_DB: /data/finance.db}
env_passthrough: [TZ]
"""

# The echo plugin as an external package: service `echo`, one plugin id.
ECHO_DESCRIPTOR = """\
schema: 1
service: echo
plugins: [echo]
manifests: [aab_plugin_echo/manifest.yaml]
runtime: "0.3"
volumes: {echo_data: /data}
env_passthrough: [TZ]
"""
DOCKERFILE = "FROM ghcr.io/pelegw/aab-plugin-base:0.3.0\nCOPY . /srv/plugin\n"

_GIT_ENV = {"GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.com",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}


def git(*args, cwd: Path, stdin: str | None = None) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, env={**os.environ, **_GIT_ENV}, input=stdin,
                       capture_output=True, text=True)
    assert r.returncode == 0, (args, r.stderr)
    return r.stdout.strip()


class RepoBuilder:
    """Builds a plugin repository commit by commit and publishes it as a
    bare repository where the Git client's url_for points."""

    def __init__(self, remote_root: Path, source: str = SOURCE):
        self.source = source
        self.work = remote_root.parent / "work" / source.replace("/", "_")
        self.bare = remote_root / f"{source}.git"
        self.work.mkdir(parents=True)
        self._links: list[tuple[str, str]] = []
        git("init", "-q", "-b", "main", str(self.work), cwd=remote_root.parent)

    def write(self, rel: str, text: str) -> "RepoBuilder":
        path = self.work / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
        return self

    def symlink(self, rel: str, target: str) -> "RepoBuilder":
        """A symlink entry in the next commit, written straight into the
        index (no OS symlink rights needed); any file at `rel` is dropped."""
        path = self.work / rel
        if path.exists():
            path.unlink()
        self._links.append((rel, target))
        return self

    def commit(self, message: str, tag: str | None = None, branch: str | None = None) -> str:
        git("add", "-A", cwd=self.work)
        for rel, target in self._links:             # after add -A, which would drop them
            blob = git("hash-object", "-w", "--stdin", cwd=self.work, stdin=target)
            git("update-index", "--add", "--cacheinfo", f"120000,{blob},{rel}", cwd=self.work)
        self._links = []
        git("commit", "-q", "--allow-empty", "-m", message, cwd=self.work)
        if tag:
            git("tag", tag, cwd=self.work)
        if branch:
            git("branch", branch, cwd=self.work)
        return git("rev-parse", "HEAD", cwd=self.work)

    def publish(self) -> "RepoBuilder":
        from aab_installer.fs import rmtree
        rmtree(self.bare)                    # git's read-only packs included (Windows)
        self.bare.parent.mkdir(parents=True, exist_ok=True)
        git("clone", "-q", "--bare", str(self.work), str(self.bare), cwd=self.work.parent)
        return self


def echo_package(builder: RepoBuilder, version: str = "0.1.0") -> RepoBuilder:
    manifest = ECHO_MANIFEST.read_text(encoding="utf-8").replace(
        "version: 0.1.0", f"version: {version}")
    return (builder.write("aab-plugin.yaml", ECHO_DESCRIPTOR).write("Dockerfile", DOCKERFILE)
            .write("aab_plugin_echo/manifest.yaml", manifest))


@pytest.fixture()
def remote(tmp_path) -> Path:
    root = tmp_path / "remote"
    root.mkdir()
    return root


@pytest.fixture()
def echo_repo(remote):
    """The echo package with v0.1.0 and v0.2.0 tags, and a branch named
    v9.9.9 (a ref that looks like a tag and is not one)."""
    b = RepoBuilder(remote)
    v1 = echo_package(b).commit("echo 0.1.0", tag="v0.1.0", branch="v9.9.9")
    v2 = echo_package(b, "0.2.0").commit("echo 0.2.0", tag="v0.2.0")
    b.publish()
    return types.SimpleNamespace(builder=b, v1=v1, v2=v2, source=SOURCE)


@pytest.fixture()
def project(tmp_path) -> Path:
    """A project root shaped like /opt/aab: the real secrets script and a
    freshly generated .env (by that script, as on a host)."""
    root = tmp_path / "aab"
    (root / "scripts").mkdir(parents=True)
    shutil.copy(REPO / "scripts" / "init_secrets.py", root / "scripts" / "init_secrets.py")
    shutil.copy(REPO / "scripts" / "compose-files.sh", root / "scripts" / "compose-files.sh")
    r = subprocess.run([sys.executable, str(root / "scripts" / "init_secrets.py"),
                        "--out", str(root / ".env")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return root


BROKER_ID = "0123456789abcdef" * 4      # what `compose ps -q broker` prints


class FakeDocker:
    """The command runner: records argv, answers `sh scripts/compose-files.sh`
    the way the script would (base file plus each plugins.d overlay), answers
    `compose ps -q broker` with BROKER_ID, and succeeds unless told to fail a
    command containing a given text (`fail[text] = exit code`) or to answer
    it with a canned (exit code, output) (`answer[text]`).
    `hold(text)` makes that command wait until `release()`."""

    def __init__(self, home: Path):
        self.home = home
        self.calls: list[list[str]] = []
        self.fail: dict[str, int] = {}
        self.answer: dict[str, tuple[int, str]] = {}
        self._held: dict[str, threading.Event] = {}
        self.entered = threading.Event()

    def __call__(self, argv, cwd, timeout):
        from aab_installer.compose import Result
        argv = list(argv)
        if argv[:2] == ["sh", "scripts/compose-files.sh"]:
            files = ["-f", "docker-compose.yml"]
            for f in sorted((self.home / "plugins.d").glob("*/compose.yml")):
                if not f.parent.name.startswith("_"):
                    files += ["-f", f"plugins.d/{f.parent.name}/compose.yml"]
            return Result(0, " ".join(files) + "\n")
        self.calls.append(argv)
        line = " ".join(argv)
        for text, event in self._held.items():
            if text in line:
                self.entered.set()
                event.wait(10)
        for text, code in self.fail.items():
            if text in line:
                return Result(code, f"simulated failure of {text}\nError: boom")
        for text, (code, output) in self.answer.items():
            if text in line:
                return Result(code, output)
        if argv[:2] == ["docker", "compose"] and argv[-3:] == ["ps", "-q", "broker"]:
            return Result(0, BROKER_ID + "\n")
        return Result(0, "done")

    def hold(self, text: str) -> None:
        self._held[text] = threading.Event()

    def release(self) -> None:
        for event in self._held.values():
            event.set()

    def commands(self) -> list[list[str]]:
        """docker commands with the project/file prefix cut off, for readable asserts."""
        out = []
        for argv in self.calls:
            if argv[:2] == ["docker", "compose"]:
                rest = argv[4:]
                files = []
                while rest[:1] == ["-f"]:
                    files.append(rest[1])
                    rest = rest[2:]
                out.append(["compose", *rest])
            else:
                out.append(argv)
        return out


@pytest.fixture()
def fake_docker(project) -> FakeDocker:
    return FakeDocker(project)


def make_settings(project: Path, allowed=("github.com/acme/*",), token=TOKEN):
    from aab_installer.config import Settings
    return Settings(token=token, allowed_sources=tuple(allowed), home=project)


def local_git(remote: Path, runner=None):
    """Git against the bare repositories under `remote`, with a place for
    the askpass script as in production (a request may carry a token)."""
    from aab_installer.git import Git
    return Git(url_for=lambda source: (remote / f"{source}.git").as_uri(), protocols=("file",),
               runner=runner, askpass_dir=remote.parent / "askpass")


@pytest.fixture()
def app(project, remote, fake_docker):
    from aab_installer.app import create_app
    return create_app(make_settings(project), git=local_git(remote), runner=fake_docker)


@pytest.fixture()
def client(app):
    return TestClient(app, headers={"X-Installer-Token": TOKEN}, raise_server_exceptions=False)


def run_job(client, path: str, body: dict) -> dict:
    """Submit a job, wait for the worker, return the finished job."""
    r = client.post(path, json=body)
    assert r.status_code == 202, r.text
    job = r.json()
    assert job["state"] == "queued"
    assert client.app.state.store.wait_idle()
    return client.get(f"/jobs/{job['id']}").json()
