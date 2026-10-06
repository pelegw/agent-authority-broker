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
