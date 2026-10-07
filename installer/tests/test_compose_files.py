"""scripts/compose-files.sh (the one list of compose files, used by deploy,
the installer and the docs) and the installer's checks on what it prints.

The script runs under a real POSIX `sh` (CI's, or Git for Windows'); the
tests are skipped where there is none."""

import shutil
import subprocess

import pytest

from aab_installer.compose import Compose, ComposeError, Result

from .conftest import REPO

SH = shutil.which("sh")
needs_sh = pytest.mark.skipif(SH is None, reason="no POSIX sh on this machine")


def files_for(root) -> list[str]:
    r = subprocess.run([SH, str(REPO / "scripts" / "compose-files.sh"), str(root)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.split()


@pytest.fixture()
def checkout(tmp_path):
    root = tmp_path / "aab"
    root.mkdir()
    return root


@needs_sh
def test_the_base_file_alone_without_an_env(checkout):
    assert files_for(checkout) == ["-f", "docker-compose.yml"]


@needs_sh
@pytest.mark.parametrize("env,expected", [
    ("SITE_DOMAIN=\nINSTALLER_ENABLED=false\n", []),
    ("SITE_DOMAIN=aab.example.com\n", ["docker-compose.public.yml"]),
    ("SITE_DOMAIN=aab.example.com\r\nINSTALLER_ENABLED=true\r\n",
     ["docker-compose.public.yml", "docker-compose.installer.yml"]),
    ("INSTALLER_ENABLED=true\n", ["docker-compose.installer.yml"]),
    ("INSTALLER_ENABLED=TRUE\n", []),                      # exactly "true", or off
    ("INSTALLER_ENABLED=true\nINSTALLER_ENABLED=false\n", []),   # the last one wins
    ("#INSTALLER_ENABLED=true\n", []),
])
def test_the_overlays_follow_the_env(checkout, env, expected):
    (checkout / ".env").write_text(env, newline="")
    out = files_for(checkout)
    assert out[:2] == ["-f", "docker-compose.yml"]
    assert out[3::2] == expected and all(f == "-f" for f in out[2::2])


@needs_sh
def test_installed_plugins_are_listed_and_strays_are_not(checkout):
    for name in ("finance", "budget", "_installer", "Bad", "has-dash", "a"):
        (checkout / "plugins.d" / name).mkdir(parents=True)
        (checkout / "plugins.d" / name / "compose.yml").write_text("services: {}\n")
    (checkout / "plugins.d" / "nocompose").mkdir()
    (checkout / "plugins.d" / "with space").mkdir()
    (checkout / "plugins.d" / "with space" / "compose.yml").write_text("services: {}\n")
    out = files_for(checkout)
    assert out == ["-f", "docker-compose.yml", "-f", "plugins.d/a/compose.yml",
                   "-f", "plugins.d/budget/compose.yml", "-f", "plugins.d/finance/compose.yml"]


@needs_sh
@pytest.mark.parametrize("env,expected", [
    ("NEWRELIC_ENABLED=true\n", ["docker-compose.newrelic.yml"]),
    ("NEWRELIC_ENABLED=TRUE\n", []),                       # exactly "true", or off
    ("NEWRELIC_ENABLED=true\r\nNEWRELIC_ENABLED=false\r\n", []),   # the last one wins
    ("NEWRELIC_ENABLED=false\n", []),
    ("SITE_DOMAIN=aab.example.com\nNEWRELIC_ENABLED=true\n",
     ["docker-compose.public.yml", "docker-compose.newrelic.yml", "ops/newrelic/public.yml"]),
    ("INSTALLER_ENABLED=true\nNEWRELIC_ENABLED=true\r\n",
     ["docker-compose.installer.yml", "docker-compose.newrelic.yml",
      "ops/newrelic/installer.yml"]),
    ("SITE_DOMAIN=x\nINSTALLER_ENABLED=true\nNEWRELIC_ENABLED=true\n",
     ["docker-compose.public.yml", "docker-compose.installer.yml",
      "docker-compose.newrelic.yml", "ops/newrelic/public.yml", "ops/newrelic/installer.yml"]),
])
def test_the_newrelic_overlay_comes_last_with_one_override_per_optional_file(checkout, env,
                                                                             expected):
    """A compose override cannot name a service no loaded file defines, so
    the edge's and the installer's logging overrides come only with the
    file that defines them, and after docker-compose.newrelic.yml."""
    (checkout / ".env").write_text(env, newline="")
    out = files_for(checkout)
    assert out[:2] == ["-f", "docker-compose.yml"]
    assert out[3::2] == expected and all(f == "-f" for f in out[2::2])


@needs_sh
def test_installed_plugins_get_their_newrelic_override_after_every_overlay(checkout):
    for name in ("finance", "budget", "old"):
        (checkout / "plugins.d" / name).mkdir(parents=True)
        (checkout / "plugins.d" / name / "compose.yml").write_text("services: {}\n")
    for name in ("finance", "budget"):         # "old" was installed before the file existed
        (checkout / "plugins.d" / name / "newrelic.yml").write_text("services: {}\n")
    # A newrelic.yml without its compose.yml (or in a stray directory) would
    # name a service no file defines: never listed.
    for name in ("orphan", "Bad"):
        (checkout / "plugins.d" / name).mkdir()
        (checkout / "plugins.d" / name / "newrelic.yml").write_text("services: {}\n")
    plugins = ["-f", "plugins.d/budget/compose.yml", "-f", "plugins.d/finance/compose.yml",
               "-f", "plugins.d/old/compose.yml"]
    assert files_for(checkout) == ["-f", "docker-compose.yml", *plugins]
    (checkout / ".env").write_text("NEWRELIC_ENABLED=true\n", newline="")
    assert files_for(checkout) == [
        "-f", "docker-compose.yml", *plugins, "-f", "docker-compose.newrelic.yml",
        "-f", "plugins.d/budget/newrelic.yml", "-f", "plugins.d/finance/newrelic.yml"]


@needs_sh
def test_the_installer_accepts_every_file_the_script_prints(checkout):
    (checkout / "plugins.d" / "finance").mkdir(parents=True)
    for name in ("compose.yml", "newrelic.yml"):
        (checkout / "plugins.d" / "finance" / name).write_text("services: {}\n")
    (checkout / ".env").write_text("SITE_DOMAIN=x\nINSTALLER_ENABLED=true\n"
                                   "NEWRELIC_ENABLED=true\n", newline="")
    printed = " ".join(files_for(checkout))
    assert len(printed.split()) == 16
    assert _compose_with(printed).files() == printed.split()


@needs_sh
def test_the_repo_checkout_itself():
    out = files_for(REPO)
    assert out[:2] == ["-f", "docker-compose.yml"]


def _compose_with(output: str, code: int = 0) -> Compose:
    return Compose(REPO, runner=lambda argv, cwd, timeout: Result(code, output))


@pytest.mark.parametrize("output", [
    "", "-f", "-f docker-compose.public.yml", "-f docker-compose.yml -f /etc/compose.yml",
    "-f docker-compose.yml -f ../x/compose.yml", "-f docker-compose.yml --env-file /x",
    "-f docker-compose.yml -f plugins.d/../compose.yml", "-f docker-compose.yml extra",
    "-f docker-compose.yml -f plugins.d/Bad/compose.yml",
    "-f docker-compose.yml -f ops/newrelic/other.yml",
    "-f docker-compose.yml -f ops/newrelic/../../etc/x.yml",
    "-f docker-compose.yml -f plugins.d/finance/other.yml",
    "-f docker-compose.yml -f ops/fluent-bit/pipeline.yaml",
])
def test_unexpected_file_lists_are_refused(output):
    with pytest.raises(ComposeError):
        _compose_with(output).files()


def test_a_failing_script_is_refused():
    with pytest.raises(ComposeError, match="exit 2"):
        _compose_with("-f docker-compose.yml", code=2).files()


def test_compose_commands_name_the_project_directory():
    c = _compose_with("-f docker-compose.yml -f docker-compose.installer.yml "
                      "-f plugins.d/finance/compose.yml\n")
    assert c.command("up", "-d", "broker") == [
        "docker", "compose", "--project-directory", str(REPO), "-f", "docker-compose.yml",
        "-f", "docker-compose.installer.yml", "-f", "plugins.d/finance/compose.yml",
        "up", "-d", "broker"]
