"""The files that configure a deployment agree with each other: the env-split
table is identical in docs/deployment.md and docs/architecture.md section
2.2, compose only references keys .env.example carries, and the third-party
credentials that moved to the console appear in no file. Compose also keeps
the isolation docs/architecture.md section 2 describes: the WhatsApp session
volume is mounted by the sidecar alone, the archive read-only elsewhere, and
each plugin service has a network of its own. The opt-in installer overlay
keeps its own boundary: aab-installer shares net_installer with the broker
alone, is the only container that mounts the Docker socket, and sees the
checkout at the same path as the host. Logging (docs/logging.md): every
service's log is rotated, every service gets the log settings, and no image
runs uvicorn with its access log on."""

import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
MOVED_TO_CONSOLE = ("TELEGRAM_BOT_TOKEN", "GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY_PATH",
                    "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET")
COMPOSE_FILES = ("docker-compose.yml", "docker-compose.public.yml",
                 "docker-compose.installer.yml")
INSTALLER = "docker-compose.installer.yml"
DOCKER_SOCKET = "/var/run/docker.sock"
# The overlay the installer renders for an external plugin (its golden file).
RENDERED_OVERLAY = "installer/tests/golden/finance.compose.yml"


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


class _ComposeLoader(yaml.SafeLoader):
    """Compose's `!reset` (the public overlay) is not YAML-core; read it as
    the plain value it wraps."""


_ComposeLoader.add_constructor("!reset", lambda loader, node: (
    loader.construct_sequence(node) if isinstance(node, yaml.SequenceNode)
    else loader.construct_mapping(node) if isinstance(node, yaml.MappingNode)
    else loader.construct_scalar(node)))


def _compose(rel: str) -> dict:
    return yaml.load(_read(rel), Loader=_ComposeLoader)


def _mounts(service: dict) -> list[tuple[str, str, bool]]:
    """(source, target, read_only) for each volume entry of a service. The
    short form is split on colons outside `${...}` (`${AAB_HOME:-/opt/aab}`
    has one of its own)."""
    out = []
    for v in service.get("volumes", []):
        if isinstance(v, dict):
            out.append((v["source"], v["target"], bool(v.get("read_only"))))
        else:
            parts = re.split(r":(?![^{]*\})", v)
            out.append((parts[0], parts[1], len(parts) > 2 and "ro" in parts[2].split(",")))
    return out


def _env_table(text: str) -> str:
    start = text.index("| Container | Receives | Must never receive |")
    return text[start:text.index("\n\n", start)]


def test_env_split_table_is_identical_in_both_docs():
    assert _env_table(_read("docs/deployment.md")) == _env_table(_read("docs/architecture.md"))


def test_compose_references_only_keys_the_env_file_has():
    keys = {line.split("=", 1)[0] for line in _read(".env.example").splitlines()
            if line and not line.startswith("#")}
    for rel in COMPOSE_FILES:
        used = set(re.findall(r"\$\{([A-Z0-9_]+)", _read(rel)))
        assert used, rel                                   # the scan is not vacuous
        assert used <= keys, (rel, sorted(used - keys))


def test_moved_credentials_are_in_no_deployment_file():
    for rel in (".env.example", *COMPOSE_FILES):
        text = _read(rel)
        for name in MOVED_TO_CONSOLE:
            assert name not in text, (rel, name)


def test_the_whatsapp_session_volume_is_mounted_by_the_sidecar_only():
    """session.db is the WhatsApp credential: its volume is in exactly one
    service's filesystem, and the sidecar is told to keep the session there."""
    for rel in COMPOSE_FILES:
        for name, svc in _compose(rel)["services"].items():
            for source, target, _ in _mounts(svc):
                if source == "wa_session":
                    assert (rel, name, target) == (
                        "docker-compose.yml", "whatsapp-sidecar", "/session")
    base = _compose("docker-compose.yml")
    assert "wa_session" in base["volumes"]
    sidecar = base["services"]["whatsapp-sidecar"]
    assert ("wa_session", "/session", False) in _mounts(sidecar)
    assert sidecar["environment"]["SESSION_DIR"] == "/session"


def test_the_archive_volume_is_read_only_outside_the_sidecar():
    base = _compose("docker-compose.yml")
    users = {name: [(t, ro) for s, t, ro in _mounts(svc) if s == "wa_data"]
             for name, svc in base["services"].items()}
    assert {n: m for n, m in users.items() if m} == {
        "whatsapp-sidecar": [("/data", False)], "plugin-whatsapp": [("/data", True)]}
    assert base["services"]["plugin-whatsapp"]["environment"]["MESSAGES_DB"] == \
        "/data/messages.db"


def test_each_plugin_service_has_a_network_of_its_own():
    """Who can reach whom: the broker every plugin service, each plugin
    service only the broker (plugin-whatsapp also its sidecar), the edge only
    the broker. No plugin shares a network with another plugin."""
    members: dict[str, set[str]] = {}
    for rel in ("docker-compose.yml", "docker-compose.public.yml"):
        doc = _compose(rel)
        for name, svc in doc["services"].items():
            # A service without `networks` would land on compose's default
            # network, shared with every other such service.
            if rel == "docker-compose.yml" or name not in _compose("docker-compose.yml")[
                    "services"]:
                assert svc.get("networks"), (rel, name)
            for net in svc.get("networks", []):
                members.setdefault(net, set()).add(name)
    assert members == {
        "edge_net": {"broker", "edge"},
        "net_whatsapp": {"broker", "plugin-whatsapp"},
        "net_github": {"broker", "plugin-github"},
        "net_google": {"broker", "plugin-google"},
        "wa_internal": {"plugin-whatsapp", "whatsapp-sidecar"},
    }
    assert set(_compose("docker-compose.yml")["networks"]) == set(members)
    # The broker finds each plugin by the name it has on that plugin's network.
    env = _compose("docker-compose.yml")["services"]["broker"]["environment"]
    for service in ("whatsapp", "github", "google"):
        assert env[f"PLUGIN_URL_{service.upper()}"] == f"http://plugin-{service}:8090"


def _members(*rels: str) -> dict[str, set[str]]:
    members: dict[str, set[str]] = {}
    for rel in rels:
        for name, svc in _compose(rel)["services"].items():
            for net in svc.get("networks", []):
                members.setdefault(net, set()).add(name)
    return members


def test_the_installer_shares_net_installer_with_the_broker_only():
    """aab-installer is root on the host (the Docker socket): only the broker
    may reach it, and it may reach nothing but the broker. No plugin, in-tree
    or rendered by the installer, is ever on net_installer."""
    members = _members(*COMPOSE_FILES)
    assert members["net_installer"] == {"broker", "aab-installer"}
    assert [net for net, names in members.items() if "aab-installer" in names] == [
        "net_installer"]
    doc = _compose(INSTALLER)
    assert set(doc["services"]) == {"aab-installer", "broker"}
    assert doc["services"]["aab-installer"]["networks"] == ["net_installer"]
    assert doc["services"]["broker"]["networks"] == ["net_installer"]
    assert set(doc["networks"]) == {"net_installer"}
    assert "ports" not in doc["services"]["aab-installer"]
    rendered = _compose(RENDERED_OVERLAY)
    for name, svc in rendered["services"].items():
        assert "net_installer" not in svc.get("networks", []), name
    for net, names in members.items():
        if net != "net_installer":
            assert "aab-installer" not in names, net


def test_the_docker_socket_is_mounted_by_the_installer_only():
    for rel in (*COMPOSE_FILES, RENDERED_OVERLAY):
        for name, svc in _compose(rel)["services"].items():
            for source, target, _ in _mounts(svc):
                if DOCKER_SOCKET in (source, target) or "docker.sock" in source:
                    assert (rel, name, source, target) == (
                        INSTALLER, "aab-installer", DOCKER_SOCKET, DOCKER_SOCKET)
    # Textually too: no other deployment file, and not the overlay template.
    for rel in ("docker-compose.yml", "docker-compose.public.yml", RENDERED_OVERLAY,
                "installer/aab_installer/overlay.py"):
        assert "docker.sock" not in _read(rel), rel
    assert _read(INSTALLER).count(f"{DOCKER_SOCKET}:{DOCKER_SOCKET}") == 1


def test_the_installer_sees_the_checkout_at_the_hosts_path_and_only_its_own_env():
    svc = _compose(INSTALLER)["services"]["aab-installer"]
    home = "${AAB_HOME:-/opt/aab}"
    assert (home, home, False) in _mounts(svc)
    assert {s for s, _, _ in _mounts(svc)} == {DOCKER_SOCKET, home}
    # Its own token, allowlist and git credential, nothing of the broker's or
    # any plugin's.
    assert set(svc["environment"]) == {"INSTALLER_TOKEN", "INSTALLER_ALLOWED_SOURCES",
                                       "INSTALLER_GIT_TOKEN", "AAB_HOME", "LOG_LEVEL",
                                       "LOG_FORMAT"}
    assert svc["environment"]["INSTALLER_GIT_TOKEN"] == "${INSTALLER_GIT_TOKEN:-}"
    assert svc["environment"]["AAB_HOME"] == home
    broker = _compose(INSTALLER)["services"]["broker"]["environment"]
    assert broker == {"INSTALLER_URL": "http://aab-installer:8070",
                      "INSTALLER_TOKEN": svc["environment"]["INSTALLER_TOKEN"]}
    # INSTALLER_TOKEN reaches the broker and the installer, no other container.
    for rel in COMPOSE_FILES:
        for name, s in _compose(rel)["services"].items():
            if "INSTALLER_TOKEN" in str(s.get("environment", {})):
                assert name in ("broker", "aab-installer"), (rel, name)
    # The private-repository credential is the installer's alone: not even the
    # broker holds it (and a rendered plugin overlay never does).
    for rel in (*COMPOSE_FILES, RENDERED_OVERLAY):
        for name, s in _compose(rel)["services"].items():
            if "INSTALLER_GIT_TOKEN" in str(s.get("environment", {})):
                assert (rel, name) == (INSTALLER, "aab-installer")
    for rel in ("docker-compose.yml", "docker-compose.public.yml", RENDERED_OVERLAY,
                "installer/aab_installer/overlay.py"):
        assert "INSTALLER_GIT_TOKEN" not in _read(rel), rel


PYTHON_SERVICES = ("broker", "plugin-whatsapp", "plugin-github", "plugin-google")


def test_every_service_logs_through_the_rotated_json_file_driver():
    """docs/logging.md: at most 5 x 10 MB per container, in every file (the
    public overlay's edge and the installer included)."""
    for rel in COMPOSE_FILES:
        for name, svc in _compose(rel)["services"].items():
            assert svc.get("logging") == {
                "driver": "json-file", "options": {"max-size": "10m", "max-file": "5"}}, \
                (rel, name)
    installer = _compose(INSTALLER)["services"]["aab-installer"]["environment"]
    assert installer["LOG_LEVEL"] == "${LOG_LEVEL:-INFO}"
    assert installer["LOG_FORMAT"] == "${LOG_FORMAT:-text}"


def test_log_level_and_format_reach_every_service_that_reads_them():
    services = _compose("docker-compose.yml")["services"]
    for name in PYTHON_SERVICES:
        env = services[name]["environment"]
        assert env["LOG_LEVEL"] == "${LOG_LEVEL:-INFO}", name
        assert env["LOG_FORMAT"] == "${LOG_FORMAT:-text}", name
    sidecar = services["whatsapp-sidecar"]["environment"]
    assert sidecar["LOG_LEVEL"] == "${LOG_LEVEL:-INFO}" and "LOG_FORMAT" not in sidecar


@pytest.mark.parametrize("dockerfile", ["broker/Dockerfile", "plugins/whatsapp/Dockerfile",
                                        "plugins/github/Dockerfile",
                                        "plugins/google/Dockerfile", "installer/Dockerfile"])
def test_every_uvicorn_cmd_turns_off_uvicorns_access_log(dockerfile):
    """Its line would carry the query string (the OAuth callback's code);
    the services write their own access line without it."""
    [cmd] = [line for line in _read(dockerfile).splitlines() if line.startswith("CMD ")]
    assert '"uvicorn"' in cmd and '"--no-access-log"' in cmd


def test_push_never_touches_installed_plugins_and_uses_the_compose_file_set():
    """deploy/push.sh: rsync --delete must exclude plugins.d/ (the installer's)
    or every installed plugin is wiped; the git-archive path never ships it
    because it is git-ignored. The remote compose command comes from
    scripts/compose-files.sh, never a hand-listed pair, and an older .env
    gains INSTALLER_TOKEN (generated) and AAB_HOME."""
    text = _read("deploy/push.sh")
    rsync = re.search(r"^\s*rsync .*?\"\$\{HOST\}:\$\{REMOTE_DIR\}/\"$", text, re.S | re.M).group(0)
    assert "--delete" in rsync and "--exclude '/plugins.d/'" in rsync
    assert "--delete-excluded" not in rsync
    assert "plugins.d/" in _read(".gitignore").splitlines()
    assert r'C="docker compose \$(sh scripts/compose-files.sh)"' in text
    assert "docker compose -f" not in text
    loop = re.search(r"for name in (.*?); do", text, re.S).group(1)
    assert "INSTALLER_TOKEN" in loop.replace("\\", " ").split()
    assert "'AAB_HOME=${REMOTE_DIR}' >> .env" in text
