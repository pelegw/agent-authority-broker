"""Runtime test fixtures: the broker's `echo` adapter served by `serve()`.

The echo adapter lives with the broker's test fixtures (it is also used
in-process there); it is loaded by path so this package's tests need nothing
from the broker package itself.
"""

import importlib.util
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from aab_plugin_runtime import serve

ECHO_DIR = Path(__file__).resolve().parents[2] / "broker" / "tests" / "fixtures" / "echo"
TOKEN = "test-plugin-token-0123456789"


def load_echo():
    spec = importlib.util.spec_from_file_location("echo_adapter", ECHO_DIR / "adapter.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


echo_mod = load_echo()


@pytest.fixture()
def key():
    return Fernet.generate_key().decode()


@pytest.fixture()
def echo():
    return echo_mod.EchoAdapter()


@pytest.fixture()
def app(tmp_path, key, echo):
    return serve([echo], TOKEN, tmp_path / "secrets", key)


@pytest.fixture()
def client(app):
    # raise_server_exceptions=False: a 5xx must come back as a response, as
    # it would over the network, so the error mapping can be asserted.
    return TestClient(app, headers={"X-Plugin-Token": TOKEN}, raise_server_exceptions=False)
