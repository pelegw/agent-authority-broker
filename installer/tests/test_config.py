"""The installer's settings come from its environment only, refuse to boot
open (empty token) or confused (relative AAB_HOME), and never show the token."""

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


def test_the_git_token_is_optional_read_from_env_and_never_shown(tmp_path):
    assert Settings.from_env({}).git_token == ""
    s = Settings.from_env({"INSTALLER_TOKEN": "x", "AAB_HOME": str(tmp_path),
                           "INSTALLER_GIT_TOKEN": " ghp_" + "a" * 36 + "\n"})
    assert s.git_token == "ghp_" + "a" * 36                    # a pasted newline is dropped
    s.check()
    assert s.git_token not in repr(s) and s.git_token not in str(s)


@pytest.mark.parametrize("bad", ["short", "has space inside it", "quote'inside0123",
                                 "semi;colon0123456", "dollar$ign0123456", "x" * 256])
def test_a_malformed_git_token_refuses_to_boot_without_showing_it(bad, tmp_path):
    s = Settings(token="x", allowed_sources=(), home=tmp_path, git_token=bad)
    with pytest.raises(RuntimeError, match="INSTALLER_GIT_TOKEN is malformed") as e:
        s.check()
    assert bad not in str(e.value)
