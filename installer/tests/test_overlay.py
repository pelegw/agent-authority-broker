"""The rendered compose overlay: byte-for-byte the golden file for the plan's
example, and for every valid descriptor the shape the in-tree plugins have:
exactly one network shared with the broker, named volumes only (no bind
mount), no published port, only the keys the template writes, interpolation
of nothing but the service's own .env entries and the allowlisted
passthrough, and no name that collides with the base stack's services,
networks or volumes."""

import re
from pathlib import Path

import pytest
import yaml

from aab_installer import descriptor, overlay

from .conftest import FINANCE, REPO

GOLDEN = Path(__file__).resolve().parent / "golden" / "finance.compose.yml"
GOLDEN_NEWRELIC = Path(__file__).resolve().parent / "golden" / "finance.newrelic.yml"

VARIANTS = [
    FINANCE,
    "schema: 1\nservice: ab\nplugins: [ab]\nmanifests: [m.yaml]\nruntime: '0.3'\n",
    """\
schema: 1
service: budget
plugins: [budget, ledger]
manifests: [a/budget.yaml, a/ledger.yaml]
runtime: "0.3.1"
build: {dockerfile: docker/Dockerfile}
volumes: {budget_data: /data, budget_cache: /var/cache/b, budget_x: /x}
environment: {A: "1", B: "two words", C: "x=y,z"}
env_passthrough: [TZ, LOG_LEVEL, LOG_FORMAT]
""",
]


def rendered(text: str) -> tuple[descriptor.Descriptor, str, dict]:
    d = descriptor.parse(text)
    out = overlay.render(d, overlay.service_dir(d.service))
    return d, out, yaml.safe_load(out)


def test_the_example_renders_exactly_the_golden_file():
    _, out, _ = rendered(FINANCE)
    assert out == GOLDEN.read_bytes().decode("utf-8")


def test_the_golden_file_shows_one_network_no_ports_no_binds_two_volumes():
    doc = yaml.safe_load(GOLDEN.read_text(encoding="utf-8"))
    plugin = doc["services"]["plugin-finance"]
    assert plugin["networks"] == ["net_finance"]
    assert "ports" not in plugin and "expose" not in plugin
    assert plugin["volumes"] == ["finance_secrets:/secrets", "finance_data:/data"]
    assert doc["volumes"] == {"finance_secrets": {}, "finance_data": {}}
    assert doc["networks"] == {"net_finance": {}}
    assert doc["services"]["broker"] == {
        "networks": ["net_finance"],
        "environment": {
            "PLUGIN_URL_FINANCE": "http://plugin-finance:8090",
            "PLUGIN_TOKEN_FINANCE": plugin["environment"]["PLUGIN_TOKEN"]}}


# Every key the template writes for the plugin service; anything else (ports,
# privileged, network_mode, devices, pid, cap_add, extra_hosts...) is absent
# because no code path can produce it.
PLUGIN_KEYS = {"build", "restart", "logging", "networks", "volumes", "cap_drop",
               "security_opt", "environment"}


@pytest.mark.parametrize("text", VARIANTS, ids=["finance", "minimal", "full"])
def test_every_overlay_has_the_in_tree_shape(text):
    d, out, doc = rendered(text)
    n = overlay.names(d.service)
    assert set(doc) == {"services", "networks", "volumes"}
    assert set(doc["services"]) == {n["compose_service"], "broker"}
    plugin = doc["services"][n["compose_service"]]
    assert set(plugin) == PLUGIN_KEYS
    assert plugin["build"] == {"context": f"./plugins.d/{d.service}/src",
                               "dockerfile": d.build.dockerfile}
    assert plugin["restart"] == "unless-stopped"
    assert plugin["logging"] == {"driver": "json-file",
                                 "options": {"max-size": "10m", "max-file": "5"}}
    assert plugin["cap_drop"] == ["ALL"]
    assert plugin["security_opt"] == ["no-new-privileges:true"]
    # Exactly one network, the service's own, which the broker joins too.
    assert plugin["networks"] == [n["network"]]
    assert doc["services"]["broker"]["networks"] == [n["network"]]
    assert doc["networks"] == {n["network"]: {}}
    # Named volumes only: every source is a declared top-level volume name.
    sources = [v.split(":", 1)[0] for v in plugin["volumes"]]
    assert all(":" in v and v.count(":") == 1 for v in plugin["volumes"])
    assert set(sources) == set(doc["volumes"])
    assert all(re.fullmatch(r"[a-z][a-z0-9_]*", s) for s in sources)
    assert f"{d.service}_secrets:/secrets" in plugin["volumes"]
    # The broker gains exactly the URL and token, and nothing else.
    assert set(doc["services"]["broker"]) == {"networks", "environment"}
    assert set(doc["services"]["broker"]["environment"]) == {n["url_env"], n["token_env"]}
    env = plugin["environment"]
    assert env["PLUGIN_SECRETS_DIR"] == "/secrets"
    assert env["PLUGIN_TOKEN"].startswith("${" + n["token_env"] + ":?")
    assert env["PLUGIN_SECRETS_KEY"].startswith("${" + n["key_env"] + ":?")
    for key, value in d.environment.items():
        assert env[key] == value
    for key in d.env_passthrough:
        assert env[key] == "${%s:-%s}" % (key, descriptor.PASSTHROUGH_DEFAULTS[key])


@pytest.mark.parametrize("text", VARIANTS, ids=["finance", "minimal", "full"])
def test_only_the_services_own_secrets_and_the_passthrough_are_interpolated(text):
    d, out, _ = rendered(text)
    n = overlay.names(d.service)
    referenced = set(re.findall(r"\$\{([A-Za-z0-9_]+)", out))
    assert referenced <= {n["token_env"], n["key_env"], *d.env_passthrough}
    assert n["token_env"] in referenced and n["key_env"] in referenced
    # No bare $VAR form either.
    assert not re.search(r"\$(?![{])", out)


@pytest.mark.parametrize("text", VARIANTS, ids=["finance", "minimal", "full"])
def test_no_overlay_name_collides_with_the_base_stack(text):
    _, _, doc = rendered(text)
    for rel in ("docker-compose.yml", "docker-compose.public.yml",
                "docker-compose.installer.yml"):
        path = REPO / rel
        if not path.exists():
            continue
        base = yaml.load(path.read_text(encoding="utf-8"), Loader=_ComposeLoader)
        assert set(doc["services"]) & set(base.get("services", {})) <= {"broker"}, rel
        assert not set(doc["networks"]) & set(base.get("networks") or {}), rel
        assert not set(doc["volumes"]) & set(base.get("volumes") or {}), rel


def test_the_overlay_only_points_at_the_services_own_directory():
    d = descriptor.parse(FINANCE)
    for wrong in ("plugins.d/other", "plugins.d", "../plugins.d/finance", "/opt/aab/x",
                  "plugins.d/finance/src"):
        with pytest.raises(ValueError):
            overlay.render(d, wrong)
    assert overlay.render(d, "plugins.d/finance") == overlay.render(d, "./plugins.d/finance")


def test_the_example_renders_exactly_the_golden_newrelic_override():
    d = descriptor.parse(FINANCE)
    assert overlay.render_newrelic(d) == GOLDEN_NEWRELIC.read_bytes().decode("utf-8")


@pytest.mark.parametrize("text", VARIANTS, ids=["finance", "minimal", "full"])
def test_the_newrelic_override_changes_the_plugins_logging_and_nothing_else(text):
    """Loaded after docker-compose.newrelic.yml: one service (the plugin's
    own), one key (logging), the shipper's fluentd block. No network, volume,
    port, environment or interpolation can come in through it."""
    d = descriptor.parse(text)
    out = overlay.render_newrelic(d)
    doc = yaml.safe_load(out)
    n = overlay.names(d.service)
    assert doc == {"services": {n["compose_service"]: {"logging": {
        "driver": "fluentd",
        "options": {"fluentd-address": "127.0.0.1:24224", "fluentd-async": "true",
                    "tag": "aab.{{.Name}}"}}}}}
    assert doc["services"][n["compose_service"]]["logging"] == overlay.FLUENTD_LOGGING
    assert "$" not in out
    # Nothing from the descriptor but the validated service name (values too
    # short to search for, like "1", are skipped).
    body = out.split("services:", 1)[1]
    for value in (*d.environment.values(), *d.volumes, *d.volumes.values(),
                  d.build.dockerfile):
        if len(value) >= 4:
            assert value not in body, value


def test_names_are_derived_in_one_place():
    assert overlay.names("finance") == {
        "compose_service": "plugin-finance", "network": "net_finance",
        "secrets_volume": "finance_secrets", "token_env": "PLUGIN_TOKEN_FINANCE",
        "key_env": "PLUGIN_SECRETS_KEY_FINANCE", "url_env": "PLUGIN_URL_FINANCE"}


class _ComposeLoader(yaml.SafeLoader):
    """Compose's `!reset` tag (the public overlay) read as its plain value."""


_ComposeLoader.add_constructor("!reset", lambda loader, node: (
    loader.construct_sequence(node) if isinstance(node, yaml.SequenceNode)
    else loader.construct_mapping(node) if isinstance(node, yaml.MappingNode)
    else loader.construct_scalar(node)))
