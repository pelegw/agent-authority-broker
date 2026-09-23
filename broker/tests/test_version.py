"""VERSION at the repo root is the single source of the version number."""

import subprocess
import sys
from pathlib import Path

import broker

VERSION_FILE = Path(__file__).resolve().parents[2] / "VERSION"


def test_package_version_comes_from_version_file():
    assert broker.__version__ == VERSION_FILE.read_text(encoding="utf-8").strip()


def test_fastapi_app_reports_the_same_version(env):
    from broker.main import api
    assert api.title == "Agent Authority Broker"
    assert api.version == broker.__version__


def test_cli_prints_the_version():
    out = subprocess.run([sys.executable, "-m", "cli.aab", "--version"],
                         capture_output=True, text=True,
                         cwd=Path(__file__).resolve().parents[1], check=True)
    assert out.stdout.strip() == f"aab {broker.__version__}"
