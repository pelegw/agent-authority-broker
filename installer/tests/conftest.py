"""Installer test fixtures: the example descriptor from the plan, and a
throwaway project root holding a real `scripts/init_secrets.py` and `.env`.

Nothing here needs Docker or the network: compose runs through an injected
command runner and git clones a local bare repository (test_app.py).
"""

import shutil
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

# The descriptor the plan documents (and the finance plugin will ship).
FINANCE = """\
schema: 1
service: finance
plugins: [finance]
manifests: [aab_plugin_finance/manifest.yaml]
runtime: "0.3"
build: {dockerfile: Dockerfile}
volumes: {finance_data: /data}
environment: {FINANCE_DB: /data/finance.db}
env_passthrough: [TZ]
"""


@pytest.fixture()
def project(tmp_path) -> Path:
    """A project root shaped like /opt/aab: the real secrets script and a
    freshly generated .env (by that script, as on a host)."""
    import subprocess
    import sys
    root = tmp_path / "aab"
    (root / "scripts").mkdir(parents=True)
    shutil.copy(REPO / "scripts" / "init_secrets.py", root / "scripts" / "init_secrets.py")
    r = subprocess.run([sys.executable, str(root / "scripts" / "init_secrets.py"),
                        "--out", str(root / ".env")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return root
