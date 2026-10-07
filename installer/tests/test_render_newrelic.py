"""`python -m aab_installer.render_newrelic`: the one-time step for a plugin
installed before the installer rendered plugins.d/<service>/newrelic.yml. It
writes the bytes an install or upgrade writes, for installed services only,
and nothing else; a bad name never becomes a path."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from aab_installer import overlay, render_newrelic

from .conftest import REPO, SOURCE
from .test_app import install, run_job

GOLDEN = Path(__file__).resolve().parent / "golden" / "finance.newrelic.yml"


def _installed(project: Path, service: str) -> Path:
    """The directory an install leaves (its record and overlay), without a
    newrelic.yml, as an install from before it existed."""
    d = project / "plugins.d" / service
    d.mkdir(parents=True)
    (d / "install.json").write_text('{"service": "%s"}' % service, encoding="utf-8")
    (d / "compose.yml").write_text("services: {}\n", encoding="utf-8")
    return d


def _main(project: Path, *argv: str) -> int:
    return render_newrelic.main(list(argv), environ={"AAB_HOME": str(project)})


def test_it_writes_the_golden_file_for_an_older_install(project, capsys):
    d = _installed(project, "finance")
    assert _main(project, "finance") == 0
    assert (d / "newrelic.yml").read_bytes() == GOLDEN.read_bytes()
    assert capsys.readouterr().out == "wrote plugins.d/finance/newrelic.yml\n"
    assert sorted(p.name for p in d.iterdir()) == ["compose.yml", "install.json", "newrelic.yml"]


def test_it_writes_what_the_installer_wrote(client, project, fake_docker, echo_repo):
    """Install, take the file away (an older install), write it again: the
    same bytes, so the next upgrade changes nothing."""
    assert install(client, echo_repo)["state"] == "done"
    path = project / "plugins.d" / "echo" / "newrelic.yml"
    rendered = path.read_bytes()
    path.unlink()
    assert _main(project, "echo") == 0
    assert path.read_bytes() == rendered == overlay.render_newrelic("echo").encode("utf-8")
    job = run_job(client, "/upgrade", {"service": "echo", "source": SOURCE, "ref": "v0.2.0",
                                       "commit": echo_repo.v2})
    assert job["state"] == "done", job
    assert path.read_bytes() == rendered


def test_all_covers_every_installed_service_and_nothing_else(project, capsys):
    a, b = _installed(project, "finance"), _installed(project, "budget")
    stray = project / "plugins.d" / "notinstalled"
    stray.mkdir()
    state = project / "plugins.d" / "_installer"
    state.mkdir()
    assert _main(project, "--all") == 0
    assert (a / "newrelic.yml").exists() and (b / "newrelic.yml").exists()
    assert list(stray.iterdir()) == [] and list(state.iterdir()) == []
    assert capsys.readouterr().out.splitlines() == [
        "wrote plugins.d/budget/newrelic.yml", "wrote plugins.d/finance/newrelic.yml"]


def test_all_with_nothing_installed_writes_nothing(project, capsys):
    assert _main(project, "--all") == 0
    assert "no installed plugin services" in capsys.readouterr().err
    assert not (project / "plugins.d").exists()


@pytest.mark.parametrize("name", ["../escape", "Finance", "broker", "a/b", "x", "edge", ""])
def test_a_name_that_is_not_an_installed_service_is_refused(project, capsys, name):
    _installed(project, "finance")
    before = sorted(str(p) for p in project.rglob("*"))
    assert _main(project, name) == 2
    assert "service" in capsys.readouterr().err
    assert sorted(str(p) for p in project.rglob("*")) == before


def test_a_service_that_is_not_installed_is_refused(project, capsys):
    (project / "plugins.d" / "ghost").mkdir(parents=True)        # no install record
    assert _main(project, "ghost") == 2
    assert "ghost is not installed" in capsys.readouterr().err
    assert not (project / "plugins.d" / "ghost" / "newrelic.yml").exists()


def test_one_bad_name_does_not_stop_the_others(project, capsys):
    d = _installed(project, "finance")
    assert _main(project, "ghost", "finance") == 2
    assert (d / "newrelic.yml").exists()


def test_it_needs_names_or_all_but_not_both(project):
    for argv in ([], ["finance", "--all"]):
        with pytest.raises(SystemExit) as e:
            _main(project, *argv)
        assert e.value.code == 2


def test_a_relative_home_is_refused(tmp_path, capsys):
    assert render_newrelic.main(["finance"], environ={"AAB_HOME": "relative/aab"}) == 2
    assert "AAB_HOME must be an absolute path" in capsys.readouterr().err


def test_it_runs_as_a_module(project):
    """The documented command line: python -m aab_installer.render_newrelic."""
    d = _installed(project, "finance")
    r = subprocess.run([sys.executable, "-m", "aab_installer.render_newrelic", "finance"],
                       capture_output=True, text=True, cwd=REPO / "installer",
                       env={**os.environ, "AAB_HOME": str(project)}, timeout=60)
    assert r.returncode == 0, r.stderr
    assert (d / "newrelic.yml").read_bytes() == GOLDEN.read_bytes()
