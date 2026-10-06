"""aab-plugin.yaml validation: the documented example and its variants are
accepted; every rule that keeps a plugin inside its own network, volumes and
environment has a rejecting row (a reserved service name, a volume outside
the service's namespace, a `$` compose would interpolate, a path leaving the
repository, an unknown key)."""

import pytest
import yaml

from aab_installer import descriptor
from aab_installer.descriptor import DescriptorError, parse

from .conftest import FINANCE

BASE = yaml.safe_load(FINANCE)


def variant(**changes) -> str:
    data = {**BASE, **changes}
    for key, value in list(data.items()):
        if value is ...:                        # `key=...` drops the key
            del data[key]
    return yaml.safe_dump(data, sort_keys=False)


def test_the_documented_example_parses():
    d = parse(FINANCE)
    assert d.service == "finance" and d.plugins == ["finance"]
    assert d.manifests == ["aab_plugin_finance/manifest.yaml"]
    assert d.runtime == "0.3" and d.build.dockerfile == "Dockerfile"
    assert d.volumes == {"finance_data": "/data"}
    assert d.environment == {"FINANCE_DB": "/data/finance.db"}
    assert d.env_passthrough == ["TZ"]
    assert descriptor.view(d) == BASE


@pytest.mark.parametrize("text", [
    variant(build=..., volumes=..., environment=..., env_passthrough=...),   # minimal
    variant(plugins=["finance", "budget"],
            manifests=["a/finance.yaml", "a/budget.yml"]),
    variant(runtime="0.3.0"),
    variant(build={"dockerfile": "docker/Dockerfile.plugin"}),
    variant(build={}),                                                       # default Dockerfile
    variant(environment={"FINANCE_DB": "/data/f.db", "MODE": "a=b c,d"}),
    variant(env_passthrough=["TZ", "LOG_LEVEL", "LOG_FORMAT"]),
    variant(volumes={"finance_data": "/data", "finance_cache": "/var/cache/finance"}),
    variant(service="ab", plugins=["ab"], volumes={"ab_x": "/x"}),
    variant(service="a" + "b" * 31, volumes={}),
])
def test_valid_variants_are_accepted(text):
    parse(text)


REJECT = [
    # schema
    ({"schema": 2}, "schema"),
    ({"schema": "1"}, "schema"),
    ({"schema": ...}, "schema"),
    # unknown keys: a repo can never add compose shape the template lacks
    ({"ports": ["8090:8090"]}, "ports"),
    ({"networks": ["net_github"]}, "networks"),
    ({"privileged": True}, "privileged"),
    ({"build": {"dockerfile": "Dockerfile", "context": ".."}}, "context"),
    ({"build": {"dockerfile": "Dockerfile", "args": {"X": "1"}}}, "args"),
    # service
    ({"service": "Finance"}, "service"),
    ({"service": "f"}, "service"),
    ({"service": "fin-ance"}, "service"),
    ({"service": "fin_ance"}, "service"),
    ({"service": "a" * 33}, "service"),
    ({"service": "1finance"}, "service"),
    ({"service": "installer"}, "already uses"),     # net_installer: the installer's network
    ({"service": "whatsapp"}, "already uses"),
    ({"service": "github"}, "already uses"),
    ({"service": "google"}, "already uses"),
    ({"service": "broker"}, "already uses"),        # broker_data
    ({"service": "wa"}, "already uses"),            # wa_session, wa_data
    ({"service": "caddy"}, "already uses"),         # caddy_data
    ({"service": "edge"}, "already uses"),
    # plugins / manifests
    ({"plugins": []}, "plugins"),
    ({"plugins": ["Finance"]}, "plugin id"),
    ({"plugins": ["fin_x"]}, "plugin id"),
    ({"plugins": ["finance", "finance"], "manifests": ["a.yaml", "b.yaml"]}, "unique"),
    ({"plugins": ["finance", "budget"]}, "one file per plugin"),
    ({"manifests": ["../manifest.yaml"]}, "relative path"),
    ({"manifests": ["a/../../manifest.yaml"]}, "relative path"),
    ({"manifests": ["/etc/manifest.yaml"]}, "relative path"),
    ({"manifests": ["a\\manifest.yaml"]}, "relative path"),
    ({"manifests": ["C:/manifest.yaml"]}, "relative path"),
    ({"manifests": ["a//manifest.yaml"]}, "relative path"),
    ({"manifests": ["./manifest.yaml"]}, "relative path"),
    ({"manifests": ["manifest.json"]}, ".yaml"),
    # runtime
    ({"runtime": 0.3}, "runtime"),                   # unquoted: a float, not a version
    ({"runtime": "latest"}, "runtime"),
    # build
    ({"build": {"dockerfile": "../Dockerfile"}}, "relative path"),
    ({"build": {"dockerfile": "/Dockerfile"}}, "relative path"),
    # volumes: named, own namespace, sane mount paths
    ({"volumes": {"data": "/data"}}, "must start with 'finance_'"),
    ({"volumes": {"finance_": "/data"}}, "must start with 'finance_'"),
    ({"volumes": {"github_secrets": "/x"}}, "must start with 'finance_'"),
    ({"volumes": {"wa_session": "/x"}}, "must start with 'finance_'"),
    ({"volumes": {"broker_data": "/x"}}, "must start with 'finance_'"),
    ({"volumes": {"finance_secrets": "/x"}}, "installer's"),
    ({"volumes": {"./data": "/data"}}, "volume name"),
    ({"volumes": {"/srv/data": "/data"}}, "volume name"),
    ({"volumes": {"finance_data": "data"}}, "absolute"),
    ({"volumes": {"finance_data": "/data:rw"}}, "absolute"),
    ({"volumes": {"finance_data": "/a/../b"}}, "absolute"),
    ({"volumes": {"finance_data": "/"}}, "may use"),
    ({"volumes": {"finance_data": "/secrets"}}, "may use"),
    ({"volumes": {"finance_data": "/secrets/sub"}}, "may use"),
    ({"volumes": {"finance_a": "/d", "finance_b": "/d/"}}, "same path"),
    ({"volumes": {f"finance_{i}": f"/v{i}" for i in range(9)}}, "volumes"),
    # environment: literal, never an installer name, never interpolated
    ({"environment": {"PLUGIN_TOKEN": "x"}}, "set by the installer"),
    ({"environment": {"PLUGIN_URL_GITHUB": "x"}}, "set by the installer"),
    ({"environment": {"lower": "x"}}, "environment name"),
    ({"environment": {"X": "${PLUGIN_TOKEN_GITHUB}"}}, "'$'"),
    ({"environment": {"X": "a$b"}}, "'$'"),
    ({"environment": {"X": 1}}, "string"),
    ({"environment": {"X": True}}, "string"),
    ({"environment": {"X": "line\nbreak"}}, "control"),
    ({"environment": {"X": "a" * 1025}}, "longer"),
    ({"environment": {"TZ": "UTC"}}, "env_passthrough"),
    # passthrough: the allowlist only
    ({"env_passthrough": ["HOME"]}, "not allowed"),
    ({"env_passthrough": ["PLUGIN_TOKEN_GITHUB"]}, "not allowed"),
    ({"env_passthrough": ["SETUP_TOKEN"]}, "not allowed"),
    ({"env_passthrough": ["TZ", "TZ"]}, "unique"),
]


@pytest.mark.parametrize("changes,fragment", REJECT,
                         ids=[f"{i}-{next(iter(c))}" for i, (c, _) in enumerate(REJECT)])
def test_invalid_descriptors_are_rejected(changes, fragment):
    with pytest.raises(DescriptorError) as e:
        parse(variant(**changes))
    assert fragment in str(e.value), str(e.value)


@pytest.mark.parametrize("text,fragment", [
    ("- a list", "mapping"),
    ("schema: [unclosed", "valid YAML"),
    ("", "mapping"),
    ("x" * (descriptor.MAX_DESCRIPTOR_BYTES + 1), "larger"),
], ids=["list", "bad-yaml", "empty", "too-large"])
def test_malformed_files_are_rejected(text, fragment):
    with pytest.raises(DescriptorError) as e:
        parse(text)
    assert fragment in str(e.value)


def test_bytes_input_must_be_utf8_and_small():
    assert parse(FINANCE.encode()).service == "finance"
    with pytest.raises(DescriptorError, match="UTF-8"):
        parse(b"\xff\xfe")
    with pytest.raises(DescriptorError, match="larger"):
        parse(b" " * (descriptor.MAX_DESCRIPTOR_BYTES + 1))


def test_errors_name_the_field():
    with pytest.raises(DescriptorError) as e:
        parse(variant(service="Bad", runtime="x"))
    msg = str(e.value)
    assert msg.startswith("service:") and "runtime:" in msg
