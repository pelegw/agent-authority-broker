"""The broker's install API against the REAL installer, served in process.

test_plugin_install.py holds the broker to the installer's HTTP contract with
a fake; this test checks that the two sides really agree: the installer app
from installer/aab_installer (create_app), a plugin repository built here as
a local bare git repository (the echo plugin packaged as an external plugin,
tagged v0.1.0 and v0.2.0, cloned over file://), and a recording stand-in for
Docker (no daemon needed). Through the broker's own routes: inspect builds
the review from what the installer read, install pins and the real job runs
to done (the .env secrets, the rendered overlay, the compose commands), the
service that comes up is registered against the pin, upgrade re-pins, remove
unpins and purges, and refusals cross the boundary with their status and code.
"""

import os
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from broker.plugins import pins, registry
from broker.plugins.registry import get_registry
from broker.services import plugin_install

from .conftest import ECHO_DIR, PLUGIN_TOKEN, runtime_factory

REPO = Path(__file__).resolve().parents[2]
# The installer is its own package at the repo root, imported from source when
# it is not installed (as conftest does for the plugin runtime). Appended, not
# prepended: installer/ also holds a `tests` package.
_INSTALLER = REPO / "installer"
try:
    import aab_installer  # noqa: F401
except ImportError:
    sys.path.append(str(_INSTALLER))

from aab_installer.app import create_app  # noqa: E402
from aab_installer.compose import Result  # noqa: E402
from aab_installer.config import Settings  # noqa: E402
from aab_installer.git import Git  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

INSTALLER_URL = "http://aab-installer:8070"
TOKEN = "contract-installer-token-0123456789abcdef0123"
SOURCE = "github.com/acme/aab-plugin-echo"
DESCRIPTOR = ("schema: 1\nservice: echo\nplugins: [echo]\n"
              "manifests: [aab_plugin_echo/manifest.yaml]\nruntime: \"0.3\"\n"
              "volumes: {echo_data: /data}\nenv_passthrough: [TZ]\n")
_GIT_ENV = {"GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.com",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}


def _git(*args: str, cwd: Path) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, env={**os.environ, **_GIT_ENV},
                       capture_output=True, text=True)
    assert r.returncode == 0, (args, r.stderr)
    return r.stdout.strip()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


class Docker:
    """Records every docker command; answers scripts/compose-files.sh the way
    the script would (the base file plus each rendered overlay)."""

    def __init__(self, home: Path):
        self.home, self.calls = home, []

    def __call__(self, argv, cwd, timeout):
        argv = list(argv)
        if argv[:2] == ["sh", "scripts/compose-files.sh"]:
            files = ["-f", "docker-compose.yml"]
            for f in sorted((self.home / "plugins.d").glob("*/compose.yml")):
                files += ["-f", f"plugins.d/{f.parent.name}/compose.yml"]
            return Result(0, " ".join(files) + "\n")
        self.calls.append(argv)
        return Result(0, "ok")

    def compose(self) -> list[list[str]]:
        """The compose sub-commands run, with the project and file flags cut off."""
        out = []
        for argv in self.calls:
            if argv[:2] == ["docker", "compose"]:
                rest = argv[4:]
                while rest[:1] == ["-f"]:
                    rest = rest[2:]
                out.append(rest)
            else:
                out.append(argv)
        return out


@pytest.fixture()
def real_installer(env, tmp_path, monkeypatch):
    """The installer app over a throwaway checkout and a local plugin repository,
    wired to the broker as INSTALLER_URL / INSTALLER_TOKEN."""
    # The echo plugin as an external package, two release tags.
    work = tmp_path / "work"
    work.mkdir()
    _git("init", "-q", "-b", "main", str(work), cwd=tmp_path)
    manifest = (ECHO_DIR / "manifest.yaml").read_text(encoding="utf-8")
    commits = {}
    for version in ("0.1.0", "0.2.0"):
        _write(work / "aab-plugin.yaml", DESCRIPTOR)
        _write(work / "Dockerfile", "FROM ghcr.io/pelegw/aab-plugin-base:0.3.0\n")
        _write(work / "aab_plugin_echo" / "manifest.yaml",
               manifest.replace("version: 0.1.0", f"version: {version}"))
        _git("add", "-A", cwd=work)
        _git("commit", "-q", "-m", f"echo {version}", cwd=work)
        _git("tag", f"v{version}", cwd=work)
        commits[version] = _git("rev-parse", "HEAD", cwd=work)
    remote = tmp_path / "remote"
    bare = remote / f"{SOURCE}.git"
    bare.parent.mkdir(parents=True)
    _git("clone", "-q", "--bare", str(work), str(bare), cwd=tmp_path)
    # A checkout shaped like /opt/aab: the real secrets script and its .env.
    home = tmp_path / "aab"
    (home / "scripts").mkdir(parents=True)
    for name in ("init_secrets.py", "compose-files.sh"):
        shutil.copy(REPO / "scripts" / name, home / "scripts" / name)
    r = subprocess.run([sys.executable, str(home / "scripts" / "init_secrets.py"), "--out",
                        str(home / ".env")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    docker = Docker(home)
    app = create_app(Settings(token=TOKEN, allowed_sources=("github.com/acme/*",), home=home),
                     git=Git(url_for=lambda s: (remote / f"{s}.git").as_uri(),
                             protocols=("file",)),
                     runner=docker)
    # The broker side: configured, echo not vendored (an external plugin).
    from broker.config import get_settings
    monkeypatch.setenv("INSTALLER_URL", INSTALLER_URL)
    monkeypatch.setenv("INSTALLER_TOKEN", TOKEN)
    get_settings.cache_clear()
    registry.reset_registry(registry.Registry(vendored_dirs=(tmp_path / "no-tree",)))
    monkeypatch.setattr(plugin_install, "client_factory", lambda base_url, headers, timeout:
                        TestClient(app, base_url=base_url, headers=headers,
                                   raise_server_exceptions=False))
    return types.SimpleNamespace(app=app, docker=docker, home=home, commits=commits)


def _run(client, headers, path, body, inst) -> dict:
    """Submit through the broker, let the installer's worker finish, read the
    job back through the broker."""
    r = client.post(path, headers=headers, json=body)
    assert r.status_code == 202, r.text
    assert inst.app.state.store.wait_idle()
    job = client.get(f"/v1/admin/plugins/install/jobs/{r.json()['job']['id']}", headers=headers)
    assert job.status_code == 200, job.text
    return job.json()


def _env_names(home: Path) -> set[str]:
    return {line.split("=", 1)[0] for line in (home / ".env").read_text().splitlines()
            if line and not line.startswith("#")}


def test_install_upgrade_and_remove_through_the_real_installer(client, admin_headers, owner,
                                                               real_installer, echo_impl,
                                                               tmp_path):
    inst = real_installer
    v1, v2 = inst.commits["0.1.0"], inst.commits["0.2.0"]
    # Inspect: the installer read the package; the broker validated and reviewed it.
    r = client.post("/v1/admin/plugins/install/inspect", headers=admin_headers,
                    json={"source": f"https://{SOURCE}.git", "ref": "v0.1.0"})
    assert r.status_code == 200, r.text
    review = r.json()
    assert (review["source"], review["commit"], review["service"]) == (SOURCE, v1, "echo")
    assert review["upgrade"] is False and review["problems"] == []
    assert review["descriptor"]["volumes"] == {"echo_data": "/data"}
    [item] = review["plugins"]
    assert item["valid"] is True and item["summary"]["secret_config"] == ["api_secret"]
    assert pins.all() == []                                   # inspecting changes nothing

    # Install: pinned, then the real job: secrets, overlay, compose.
    job = _run(client, admin_headers, "/v1/admin/plugins/install",
               {"source": SOURCE, "ref": "v0.1.0", "commit": v1}, inst)
    assert job["state"] == "done", job
    assert job["kind"] == "install" and job["service"] == "echo" and job["commit"] == v1
    assert inst.docker.compose() == [["up", "-d", "--build", "plugin-echo"], ["up", "-d", "broker"]]
    assert (pins.record("echo")["commit"], pins.record("echo")["pinned_by"]) == (v1, owner.username)
    assert (inst.home / "plugins.d" / "echo" / "compose.yml").is_file()
    assert {"PLUGIN_TOKEN_ECHO", "PLUGIN_SECRETS_KEY_ECHO"} <= _env_names(inst.home)
    [record] = client.get("/v1/admin/plugins/installed", headers=admin_headers).json()["items"]
    assert (record["service"], record["source"], record["ref"], record["commit"]) == (
        "echo", SOURCE, "v0.1.0", v1)
    assert record["plugins"] == ["echo"] and record["volumes"] == ["echo_secrets", "echo_data"]

    # The recreated broker discovers the service against the pin: disabled.
    from aab_plugin_runtime import serve
    from cryptography.fernet import Fernet
    runtime = serve([echo_impl], PLUGIN_TOKEN, tmp_path / "echo-secrets",
                    Fernet.generate_key().decode())
    get_registry().discover({"echo": ("http://plugin-echo:8090", PLUGIN_TOKEN)},
                            client_factory=runtime_factory(runtime))
    view = client.get("/v1/admin/plugins/echo", headers=admin_headers).json()
    assert view["service"] == "echo" and view["enabled"] is False

    # Installing it again is refused before anything is pinned or queued.
    r = client.post("/v1/admin/plugins/install", headers=admin_headers,
                    json={"source": SOURCE, "ref": "v0.1.0", "commit": v1})
    assert r.status_code == 409 and r.json()["code"] == "already_installed"

    # Upgrade: the review shows the diff, the new pin is written, the job runs.
    review = client.post("/v1/admin/plugins/install/inspect", headers=admin_headers,
                         json={"source": SOURCE, "ref": "v0.2.0"}).json()
    assert review["upgrade"] is True and review["installed"]["commit"] == v1
    assert review["plugins"][0]["diff"]["from_version"] == "0.1.0"
    job = _run(client, admin_headers, "/v1/admin/plugins/echo/upgrade",
               {"source": SOURCE, "ref": "v0.2.0", "commit": v2}, inst)
    assert job["state"] == "done", job
    assert pins.record("echo")["version"] == "0.2.0" and pins.record("echo")["commit"] == v2

    # Remove with purge: the installer stops it and deletes its files, volumes
    # and secrets; the broker unpins it and agents lose it at once.
    inst.docker.calls.clear()
    job = _run(client, admin_headers, "/v1/admin/plugins/echo/remove", {"purge": True}, inst)
    assert job["state"] == "done", job
    assert inst.docker.compose()[0] == ["rm", "-s", "-f", "plugin-echo"]
    assert ["docker", "volume", "rm", "aab_echo_secrets"] in inst.docker.calls
    assert pins.record("echo") is None
    assert client.get("/v1/admin/plugins/echo", headers=admin_headers).status_code == 404
    assert not (inst.home / "plugins.d" / "echo").exists()
    assert not {"PLUGIN_TOKEN_ECHO", "PLUGIN_SECRETS_KEY_ECHO"} & _env_names(inst.home)
    assert client.get("/v1/admin/plugins/installed", headers=admin_headers).json() == {"items": []}


def test_refusals_cross_the_boundary_with_status_and_code(client, admin_headers, real_installer):
    r = client.post("/v1/admin/plugins/install/inspect", headers=admin_headers,
                    json={"source": "github.com/someone-else/aab-plugin-echo", "ref": "v0.1.0"})
    assert r.status_code == 403 and r.json()["code"] == "source_not_allowed"
    r = client.post("/v1/admin/plugins/install/inspect", headers=admin_headers,
                    json={"source": SOURCE, "ref": "main"})
    assert r.status_code == 400 and r.json()["code"] == "bad_request"
    r = client.post("/v1/admin/plugins/install/inspect", headers=admin_headers,
                    json={"source": SOURCE, "ref": "v9.9.9"})
    assert r.status_code == 502 and r.json()["code"] == "clone_failed"
    # A commit other than the one the tag resolves to: refused by the broker
    # before anything is pinned.
    r = client.post("/v1/admin/plugins/install", headers=admin_headers,
                    json={"source": SOURCE, "ref": "v0.1.0",
                          "commit": real_installer.commits["0.2.0"]})
    assert r.status_code == 409 and r.json()["code"] == "commit_changed"
    assert pins.all() == [] and real_installer.docker.calls == []
    r = client.post("/v1/admin/plugins/echo/remove", headers=admin_headers, json={})
    assert r.status_code == 404 and r.json()["code"] == "not_installed"
