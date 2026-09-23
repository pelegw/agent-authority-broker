"""Fixtures for the authority suite: loaded manifests, a principal row, a key
factory, and the hypothesis profiles.

Hypothesis profiles: `ci` (default) is derandomized with 200 examples so CI
is reproducible; `HYPOTHESIS_PROFILE=dev` runs 1000 random examples locally.
deadline=None because examples touch SQLite and timing varies by machine.
"""

import os

import pytest
from hypothesis import HealthCheck, settings

from .helpers import ECHO, GITHUB, WHATSAPP, insert_principal, make_key

settings.register_profile(
    "ci", derandomize=True, max_examples=200, deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
settings.register_profile(
    "dev", max_examples=1000, deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "ci"))


@pytest.fixture()
def manifests():
    return {m.id: m for m in (ECHO, WHATSAPP, GITHUB)}


@pytest.fixture()
def principal(env):
    """The owner principal row (phase 1 creates it for real; here we insert it)."""
    return insert_principal()


@pytest.fixture()
def key_factory(principal):
    """make(name=..., role=..., parent=...) -> NewKey for the test principal."""
    def make(**kw):
        return make_key(principal, **kw)
    return make
