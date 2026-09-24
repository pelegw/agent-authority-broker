"""The three packaged manifests: the properties the rest of the plugin relies on.

(The broker validates them with its own loader and pins the vendored copies
byte for byte in broker/tests/targets/test_google.py.)"""

import re

import pytest

from aab_plugin_google.scopes import FULL_MAIL_URL, manifest_scopes, scope_name, scope_url

from .conftest import manifest

PLUGINS = ("gmail", "gcal", "gdrive")
READ_SCOPES = {"gmail": {"gmail.readonly"}, "gcal": {"calendar.readonly"},
               "gdrive": {"drive.readonly"}}


@pytest.mark.parametrize("plugin", PLUGINS)
def test_reads_need_only_the_read_only_scope(plugin):
    for a in manifest(plugin)["actions"]:
        scopes = set(a["target_permissions"])
        assert scopes, f"{a['name']} has no target_permissions"
        if a["side_effect"] == "read":
            assert scopes == READ_SCOPES[plugin], a["name"]
        else:
            assert not scopes & READ_SCOPES[plugin], a["name"]


@pytest.mark.parametrize("plugin", PLUGINS)
def test_the_shared_google_account_config(plugin):
    m = manifest(plugin)
    assert m["connection"] == {"kind": "google_oauth", "shared": "google",
                               "enforcement": "target"}
    fields = {f["name"]: f for f in m["config_schema"]}
    assert set(fields) == {"client_id", "client_secret"}
    assert fields["client_id"]["shared"] and not fields["client_id"].get("secret")
    assert fields["client_secret"]["shared"] and fields["client_secret"]["secret"]
    # Configuration principle: nothing refers to an env variable.
    assert not re.search(r"\$\{|GOOGLE_OAUTH|SITE_DOMAIN", str(m))


@pytest.mark.parametrize("plugin", PLUGINS)
def test_scopes_are_derived_and_target_enforced(plugin):
    [scopes] = [n for n in manifest(plugin)["narrowings"] if n["dimension"] == "scopes"]
    assert scopes["derived_from"] == "target_permissions" and scopes["enforcement"] == "target"
    for n in manifest(plugin)["narrowings"]:
        if n["dimension"] != "scopes":
            assert n["enforcement"] == "proxy" and n["form"] in ("list", "subtree", "pattern")


@pytest.mark.parametrize("plugin", PLUGINS)
def test_constraints_are_scalar(plugin):
    assert {c["form"] for c in manifest(plugin)["constraints"]} <= {"range", "flag", "level"}


@pytest.mark.parametrize("plugin", PLUGINS)
def test_flags_are_phrased_so_true_is_permissive(plugin):
    """The algebra treats an absent flag as true and DROPS true as top, so a
    flag whose true value restricts (hide_private, metadata_only,
    own_events_only) would silently vanish from every grant. Guard the names."""
    for c in manifest(plugin)["constraints"]:
        if c["form"] == "flag":
            assert not re.search(r"^(hide|no|block|deny|only)_|_only$|^metadata_only$",
                                 c["name"]), c["name"]


def test_hide_keyword_is_not_declared():
    assert "hide_keyword" not in {c["name"] for c in manifest("gcal")["constraints"]}


def test_scope_names_round_trip():
    for plugin in PLUGINS:
        for name in manifest_scopes(manifest(plugin)):
            assert scope_name(scope_url(name)) == name
    assert scope_url("mail.google.com") == FULL_MAIL_URL
    for bad in ("", "Gmail", "gmail..readonly", "https://evil/x"):
        with pytest.raises(ValueError):
            scope_url(bad)
