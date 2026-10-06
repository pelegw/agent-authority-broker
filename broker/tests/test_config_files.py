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
runs uvicorn with its access log on. The opt-in New Relic overlay: the
license key reaches the log shipper alone, every service's logging is
switched to the shipper (the edge, the installer and installed plugins
through their own override files), the audit exporter mounts broker_data
read-only with no network, and nothing is published but the shipper's
loopback port."""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
MOVED_TO_CONSOLE = ("TELEGRAM_BOT_TOKEN", "GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY_PATH",
                    "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET",
                    "INSTALLER_GIT_TOKEN")
COMPOSE_FILES = ("docker-compose.yml", "docker-compose.public.yml",
                 "docker-compose.installer.yml")
INSTALLER = "docker-compose.installer.yml"
DOCKER_SOCKET = "/var/run/docker.sock"
# The overlay the installer renders for an external plugin (its golden file).
RENDERED_OVERLAY = "installer/tests/golden/finance.compose.yml"
# The New Relic overlay, and the logging override of each service it cannot
# name itself, with the file that defines that service.
NEWRELIC = "docker-compose.newrelic.yml"
NEWRELIC_OVERRIDES = {"ops/newrelic/public.yml": ("docker-compose.public.yml", "edge"),
                      "ops/newrelic/installer.yml": (INSTALLER, "aab-installer"),
                      "installer/tests/golden/finance.newrelic.yml": (RENDERED_OVERLAY,
                                                                      "plugin-finance")}
NEWRELIC_FILES = (NEWRELIC, *NEWRELIC_OVERRIDES)
LICENSE_KEY = "NEW_RELIC_LICENSE_KEY"
FLUENTD = {"driver": "fluentd", "options": {"fluentd-address": "127.0.0.1:24224",
                                            "fluentd-async": "true", "tag": "aab.{{.Name}}"}}
ROTATED = {"driver": "json-file", "options": {"max-size": "10m", "max-file": "5"}}


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
    for rel in (*COMPOSE_FILES, NEWRELIC):
        used = set(re.findall(r"\$\{([A-Z0-9_]+)", _read(rel)))
        assert used, rel                                   # the scan is not vacuous
        assert used <= keys, (rel, sorted(used - keys))


def test_moved_credentials_are_in_no_deployment_file():
    for rel in (".env.example", *COMPOSE_FILES, *NEWRELIC_FILES):
        text = _read(rel)
        for name in MOVED_TO_CONSOLE:
            assert name not in text, (rel, name)


def test_the_whatsapp_session_volume_is_mounted_by_the_sidecar_only():
    """session.db is the WhatsApp credential: its volume is in exactly one
    service's filesystem, and the sidecar is told to keep the session there."""
    for rel in (*COMPOSE_FILES, *NEWRELIC_FILES):
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
    for rel in (*COMPOSE_FILES, RENDERED_OVERLAY, *NEWRELIC_FILES):
        for name, svc in _compose(rel)["services"].items():
            for source, target, _ in _mounts(svc):
                if DOCKER_SOCKET in (source, target) or "docker.sock" in source:
                    assert (rel, name, source, target) == (
                        INSTALLER, "aab-installer", DOCKER_SOCKET, DOCKER_SOCKET)
    # Textually too: no other deployment file, and not the overlay template.
    for rel in ("docker-compose.yml", "docker-compose.public.yml", RENDERED_OVERLAY,
                "installer/aab_installer/overlay.py", *NEWRELIC_FILES):
        assert "docker.sock" not in _read(rel), rel
    assert _read(INSTALLER).count(f"{DOCKER_SOCKET}:{DOCKER_SOCKET}") == 1


def test_the_installer_sees_the_checkout_at_the_hosts_path_and_only_its_own_env():
    svc = _compose(INSTALLER)["services"]["aab-installer"]
    home = "${AAB_HOME:-/opt/aab}"
    assert (home, home, False) in _mounts(svc)
    assert {s for s, _, _ in _mounts(svc)} == {DOCKER_SOCKET, home}
    # Its own token and allowlist, nothing of the broker's or any plugin's,
    # and no git credential (the broker sends one per request when the owner
    # stored it in the console).
    assert set(svc["environment"]) == {"INSTALLER_TOKEN", "INSTALLER_ALLOWED_SOURCES",
                                       "AAB_HOME", "LOG_LEVEL", "LOG_FORMAT"}
    assert svc["environment"]["AAB_HOME"] == home
    broker = _compose(INSTALLER)["services"]["broker"]["environment"]
    assert broker == {"INSTALLER_URL": "http://aab-installer:8070",
                      "INSTALLER_TOKEN": svc["environment"]["INSTALLER_TOKEN"]}
    # INSTALLER_TOKEN reaches the broker and the installer, no other container.
    for rel in (*COMPOSE_FILES, *NEWRELIC_FILES):
        for name, s in _compose(rel)["services"].items():
            if "INSTALLER_TOKEN" in str(s.get("environment", {})):
                assert name in ("broker", "aab-installer"), (rel, name)
    # The private-repository credential is a console setting, never env
    # (MOVED_TO_CONSOLE): not in the rendered overlay or its template either.
    for rel in (RENDERED_OVERLAY, "installer/aab_installer/overlay.py",
                "scripts/init_secrets.py"):
        assert "GIT_TOKEN" not in _read(rel), rel


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


# ---- the New Relic overlay (docs/logging.md, "Shipping logs and the audit record") ----

def _all_services() -> dict[str, str]:
    """Every service any deployment can run, with the file defining it."""
    out = {}
    for rel in (*COMPOSE_FILES, RENDERED_OVERLAY, NEWRELIC):
        doc = _compose(rel)
        for name, svc in doc["services"].items():
            if "build" in svc or "image" in svc:
                out.setdefault(name, rel)
    return out


def test_the_license_key_reaches_the_log_shipper_alone():
    """The one third-party credential in .env: compose hands it to the
    log-shipper container and nothing else, under its own name only. It is
    an env reference in the Fluent Bit config, never a value in a file."""
    holders = []
    for rel in (*COMPOSE_FILES, RENDERED_OVERLAY, *NEWRELIC_FILES):
        for name, svc in _compose(rel)["services"].items():
            if LICENSE_KEY in json.dumps(svc):
                holders.append((rel, name))
    assert holders == [(NEWRELIC, "log-shipper")]
    env = _compose(NEWRELIC)["services"]["log-shipper"]["environment"]
    assert list(env) == [LICENSE_KEY]
    # `:?`: compose refuses to start the overlay without it, never an empty key.
    assert env[LICENSE_KEY].startswith("${" + LICENSE_KEY + ":?")
    for rel in (*COMPOSE_FILES, RENDERED_OVERLAY, *NEWRELIC_OVERRIDES):
        assert LICENSE_KEY not in _read(rel), rel
    for region in ("US", "EU"):
        text = _read(f"ops/fluent-bit/region-{region}.yaml")
        assert "license_key: ${NEW_RELIC_LICENSE_KEY}" in text
    assert f"{LICENSE_KEY}=\n" in _read(".env.example")


def test_every_service_logs_to_the_shipper_under_the_overlay():
    """With the overlay loaded, every container's log driver is the
    shipper's fluentd block: the base services in the overlay itself, the
    edge, the installer and an installed plugin in their own override files,
    loaded only with the file that defines them. The shipper is the one
    exception: its own log stays local (it would loop through itself)."""
    overlay = _compose(NEWRELIC)
    assert overlay["x-fluentd"] == FLUENTD
    covered = {}
    for name, svc in overlay["services"].items():
        covered[name] = svc["logging"]
        if name in _compose("docker-compose.yml")["services"]:
            assert set(svc) == {"logging"}, name           # logging only, nothing else
    for rel, (defined_in, service) in NEWRELIC_OVERRIDES.items():
        doc = _compose(rel)
        assert doc == {"services": {service: {"logging": FLUENTD}}}, rel
        assert service in _compose(defined_in)["services"], rel
        covered[service] = doc["services"][service]["logging"]
    services = _all_services()
    assert set(covered) == set(services), sorted(set(services) ^ set(covered))
    for name, logging in covered.items():
        assert logging == (ROTATED if name == "log-shipper" else FLUENTD), name
    assert {"broker", "edge", "aab-installer", "plugin-finance", "whatsapp-sidecar",
            "audit-exporter", "log-shipper"} <= set(services)


def test_the_audit_exporter_reads_broker_data_read_only_with_no_network():
    svc = _compose(NEWRELIC)["services"]["audit-exporter"]
    assert sorted(_mounts(svc)) == [("audit_export_state", "/audit-export", False),
                                    ("broker_data", "/gwdata", True)]
    assert svc["network_mode"] == "none" and "networks" not in svc
    assert "ports" not in svc and "expose" not in svc
    assert svc["read_only"] is True
    assert svc["cap_drop"] == ["ALL"] and svc["security_opt"] == ["no-new-privileges:true"]
    assert svc["healthcheck"] == {"disable": True}
    assert svc["command"] == ["aab", "audit", "export", "--loop"]
    # The broker's own image, and no secret of anyone's.
    assert svc["build"] == _compose("docker-compose.yml")["services"]["broker"]["build"]
    assert set(svc["environment"]) == {"BROKER_DB", "AUDIT_EXPORT_STATE", "AUDIT_EXPORT_INTERVAL",
                                       "AUDIT_EXPORT_HASH_RESOURCES", "LOG_LEVEL", "LOG_FORMAT"}
    assert svc["environment"]["BROKER_DB"] == "/gwdata/broker.db"
    assert svc["environment"]["AUDIT_EXPORT_STATE"].startswith("/audit-export/")
    # broker_data is writable in the broker alone, everywhere.
    for rel in (*COMPOSE_FILES, RENDERED_OVERLAY, *NEWRELIC_FILES):
        for name, s in _compose(rel)["services"].items():
            for source, _, read_only in _mounts(s):
                if source == "broker_data":
                    assert name == "broker" or read_only, (rel, name)
    # The image owns the cursor directory, so the fresh volume does too.
    dockerfile = _read("broker/Dockerfile")
    assert "mkdir -p /gwdata /audit-export" in dockerfile
    assert "chown aab:aab /gwdata /audit-export" in dockerfile


def test_the_log_shipper_is_alone_and_publishes_only_its_loopback_port():
    doc = _compose(NEWRELIC)
    shipper = doc["services"]["log-shipper"]
    assert re.fullmatch(r"fluent/fluent-bit:\d+\.\d+\.\d+", shipper["image"])   # pinned
    assert shipper["ports"] == ["127.0.0.1:24224:24224"]
    assert shipper["networks"] == ["net_logs"]
    assert _mounts(shipper) == [("./ops/fluent-bit", "/fluent-bit/etc/aab", True)]
    assert shipper["user"] == "65534:65534" and shipper["read_only"] is True
    assert shipper["cap_drop"] == ["ALL"]
    assert shipper["command"] == ["-c", "/fluent-bit/etc/aab/region-${NEW_RELIC_REGION:-US}.yaml"]
    # Nothing else in any New Relic file publishes a port or joins net_logs.
    for rel in NEWRELIC_FILES:
        for name, svc in _compose(rel)["services"].items():
            if (rel, name) != (NEWRELIC, "log-shipper"):
                assert "ports" not in svc and "expose" not in svc, (rel, name)
    members = _members(*COMPOSE_FILES, RENDERED_OVERLAY, NEWRELIC)
    assert members["net_logs"] == {"log-shipper"}
    assert [net for net, names in members.items() if "log-shipper" in names] == ["net_logs"]
    assert set(doc["networks"]) == {"net_logs"}
    assert set(doc["volumes"]) == {"audit_export_state"}
    # The driver's address is the published one.
    assert FLUENTD["options"]["fluentd-address"] == shipper["ports"][0].rsplit(":", 1)[0]


def test_the_two_region_files_differ_only_in_the_endpoint():
    us = _read("ops/fluent-bit/region-US.yaml")
    eu = _read("ops/fluent-bit/region-EU.yaml")
    assert "base_uri: https://log-api.newrelic.com/log/v1" in us
    assert "base_uri: https://log-api.eu.newrelic.com/log/v1" in eu
    assert eu.replace("log-api.eu.newrelic.com", "log-api.newrelic.com").replace(
        "EU region (NEW_RELIC_REGION=EU)", "US region (NEW_RELIC_REGION=US, the default)").replace(
        "same file as\n# region-US.yaml", "same file as\n# region-EU.yaml") == us
    for text in (us, eu):
        out = yaml.safe_load(text)
        assert out["includes"] == ["pipeline.yaml"]
        [output] = out["pipeline"]["outputs"]
        assert output["name"] == "nrlogs" and output["match"] == "aab.*"
    assert sorted(p.name for p in (REPO / "ops" / "fluent-bit").glob("region-*.yaml")) == [
        "region-EU.yaml", "region-US.yaml"]


def _sidecar_filter() -> re.Pattern:
    pipeline = yaml.safe_load(_read("ops/fluent-bit/pipeline.yaml"))["pipeline"]
    [grep] = [f for f in pipeline["filters"] if f["name"] == "grep"]
    assert re.match(grep["match_regex"], "aab.aab-whatsapp-sidecar-1")
    assert not re.match(grep["match_regex"], "aab.aab-broker-1")
    key, pattern = grep["regex"].split(" ", 1)
    assert key == "log"
    return re.compile(pattern)


@pytest.mark.parametrize("line,shipped", [
    ("2026/09/24 20:31:35.601156 request method=GET path=/status status=200", True),
    ("2026/09/24 20:31:36.000001 qr event=code", True),
    ("20:31:35.601 [WhatsApp INFO] Connected", True),
    ("█▀▄ ██▀▀ ▄█", False),     # a QR row
    ("    ██▀▀▄", False),
    ("==== Scan this QR with WhatsApp (Settings > Linked devices) ====", False),
    ("(also available as PNG via the broker: GET /v1/admin/plugins/whatsapp/connect/qr.png)",
     False),
    ("", False),
])
def test_the_pairing_qr_never_leaves_the_server(line, shipped):
    """The sidecar prints the QR for an operator on this server; the shipper
    keeps only the sidecar's real log lines (an allowlist, so anything new it
    prints stays local too). Same regex semantics as Fluent Bit's for this
    pattern."""
    assert bool(_sidecar_filter().search(line)) is shipped


def _docker_compose() -> list[str] | None:
    docker = shutil.which("docker")
    if docker is None:
        return None
    try:
        r = subprocess.run([docker, "compose", "version"], capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return [docker, "compose"] if r.returncode == 0 else None


@pytest.mark.skipif(_docker_compose() is None, reason="no docker compose on this machine")
@pytest.mark.parametrize("region", ["US", "EU"])
def test_compose_merges_the_whole_stack_as_these_tests_read_it(tmp_path, region):
    """`docker compose config` (compose's own merge, no daemon needed) over
    every file a deployment can load, with a throwaway .env: the overlay is
    valid, every service but the shipper logs to it, only the edge and the
    shipper's loopback port are published, and the region picks the file."""
    env = tmp_path / ".env"
    script = str(REPO / "scripts" / "init_secrets.py")
    for args in ([], ["--rotate", "PLUGIN_TOKEN_FINANCE"],
                 ["--rotate", "PLUGIN_SECRETS_KEY_FINANCE"]):
        subprocess.run([sys.executable, script, "--out", str(env), *args], check=True,
                       capture_output=True)
    with env.open("a", encoding="utf-8", newline="\n") as f:
        f.write("SITE_DOMAIN=aab.example.com\nINSTALLER_ENABLED=true\nNEWRELIC_ENABLED=true\n"
                f"NEW_RELIC_REGION={region}\n"
                # Obviously fake, and not shaped like a real key.
                f"{LICENSE_KEY}=fake-license-key-for-a-config-test\n")
    files = ["docker-compose.yml", "docker-compose.public.yml", INSTALLER, RENDERED_OVERLAY,
             *NEWRELIC_FILES]
    argv = [*_docker_compose(), "--project-directory", str(REPO), "--env-file", str(env)]
    for rel in files:
        argv += ["-f", rel]
    run_env = {**os.environ, "MSYS_NO_PATHCONV": "1"}
    r = subprocess.run([*argv, "config", "--format", "json"], capture_output=True,
                       text=True, cwd=REPO, timeout=120, env=run_env)
    assert r.returncode == 0, r.stderr
    merged = json.loads(r.stdout)["services"]
    assert set(merged) == set(_all_services())
    for name, svc in merged.items():
        expected = ROTATED if name == "log-shipper" else FLUENTD
        assert svc["logging"] == expected, name
        published = [(p.get("host_ip", ""), str(p["published"])) for p in svc.get("ports", [])]
        assert published == {"edge": [("", "443")],
                             "log-shipper": [("127.0.0.1", "24224")]}.get(name, []), name
    assert merged["log-shipper"]["command"] == ["-c", f"/fluent-bit/etc/aab/region-{region}.yaml"]
    assert merged["log-shipper"]["environment"] == {
        LICENSE_KEY: "fake-license-key-for-a-config-test"}
    assert merged["audit-exporter"]["network_mode"] == "none"
