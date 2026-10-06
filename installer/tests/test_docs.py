"""docs/plugin-packaging.md documents the descriptor exactly as implemented:
its example is a valid descriptor (the finance plugin's, as the tests use it),
its rule table names every field and every reserved service name, and the
overlay it shows says what the renderer really writes."""

import re

import yaml

from aab_installer.descriptor import RESERVED_SERVICES, Descriptor, parse
from aab_installer.overlay import document

from .conftest import FINANCE, REPO

DOC = (REPO / "docs" / "plugin-packaging.md").read_text(encoding="utf-8")


def _section(title: str) -> str:
    start = DOC.index(f"## {title}")
    end = DOC.find("\n## ", start + 1)
    return DOC[start:end if end != -1 else len(DOC)]


def test_the_descriptor_example_is_the_finance_descriptor():
    block = re.search(r"```yaml\n(.*?)```", _section("The descriptor"), re.S).group(1)
    assert parse(block) == parse(FINANCE)


def test_the_rule_table_names_every_field_and_reserved_name():
    section = _section("The descriptor")
    for field in Descriptor.model_fields:
        name = "schema" if field == "schema_" else field
        assert f"| `{name}` |" in section, name
    for name in RESERVED_SERVICES:
        assert f"`{name}`" in section, name


def test_the_overlay_shown_is_what_the_renderer_writes():
    block = re.search(r"```yaml\n(.*?)```", _section("What the installer renders"), re.S).group(1)
    shown = yaml.safe_load(block.replace("...}'", "}'"))
    real = document(parse(FINANCE), "plugins.d/finance")
    for key in ("networks", "volumes"):
        assert shown[key] == real[key], key
    plugin, real_plugin = shown["services"]["plugin-finance"], real["services"]["plugin-finance"]
    for key in ("build", "restart", "networks", "volumes", "cap_drop", "security_opt"):
        assert plugin[key] == real_plugin[key], key
    assert set(plugin["environment"]) == set(real_plugin["environment"])
    assert shown["services"]["broker"]["networks"] == real["services"]["broker"]["networks"]
    assert set(shown["services"]["broker"]["environment"]) == set(
        real["services"]["broker"]["environment"])
