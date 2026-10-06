"""The installer's settings come from its environment only, refuse to boot
open (empty token) or confused (relative AAB_HOME), never show the token, and
hold no git credential: the GitHub token arrives per request from the broker."""

import dataclasses
from pathlib import Path

import pytest

from aab_installer.config import Settings


def test_settings_from_env():
    s = Settings.from_env({"INSTALLER_TOKEN": "t0k3n", "AAB_HOME": "/srv/aab",
                           "INSTALLER_ALLOWED_SOURCES": "github.com/pelegw/*, bogus"})
    assert s.token == "t0k3n" and s.home == Path("/srv/aab")
    assert s.allowed_sources == ("github.com/pelegw/*",)
    assert s.plugins_dir == Path("/srv/aab/plugins.d")
    assert s.state_dir == Path("/srv/aab/plugins.d/_installer")


def test_defaults_fail_closed():
    s = Settings.from_env({})
    assert s.token == "" and s.allowed_sources == () and s.home == Path("/opt/aab")
    with pytest.raises(RuntimeError, match="INSTALLER_TOKEN"):
        s.check()


def test_a_relative_home_is_refused(tmp_path):
    with pytest.raises(RuntimeError, match="absolute"):
        Settings(token="x", allowed_sources=(), home=Path("relative/aab")).check()
    Settings(token="x", allowed_sources=(), home=tmp_path).check()


def test_the_token_never_shows_in_a_repr():
    s = Settings(token="very-secret-token", allowed_sources=(), home=Path("/opt/aab"))
    assert "very-secret-token" not in repr(s) and "very-secret-token" not in str(s)


def test_no_git_credential_is_read_from_the_environment(tmp_path):
    """A leftover INSTALLER_GIT_TOKEN in an old environment is ignored: the
    settings have no place for a git credential at all."""
    leftover = "leftover-git-token-0123456789abcdef"
    s = Settings.from_env({"INSTALLER_TOKEN": "x", "AAB_HOME": str(tmp_path),
                           "INSTALLER_GIT_TOKEN": leftover})
    s.check()
    assert {f.name for f in dataclasses.fields(Settings)} == {"token", "allowed_sources",
                                                              "home"}
    assert leftover not in repr(s) and leftover not in str(vars(s))
