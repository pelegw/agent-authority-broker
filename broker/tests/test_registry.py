"""Plugin registry: env discovery, manifest pinning, plugins rows, states,
the lattice singleton, and the RemoteAdapter error contract."""

import httpx
import pytest
from cryptography.fernet import Fernet

from aab_plugin_runtime import serve
from broker import db
from broker.config import plugin_services
from broker.plugins import registry, settings
from broker.plugins.adapter import AdapterError, CallScope, RemoteAdapter, request
from broker.plugins.registry import get_registry

from .conftest import (PLUGIN_TOKEN, echo_manifest, enable_plugin, register_remote,
                       runtime_factory)


def _audit(action):
    with db.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM audit_log WHERE action = ?",
                                              (action,))]


def test_plugin_services_from_env():
    env = {"PLUGIN_URL_WHATSAPP": "http://plugin-whatsapp:8080/", "PLUGIN_TOKEN_WHATSAPP": "t1",
           "PLUGIN_URL_GOOGLE": "http://plugin-google:8080", "PLUGIN_TOKEN_GOOGLE": "",
           "PLUGIN_URL_GITHUB": "", "OTHER": "x"}
    # No token, no service: the broker never calls a plugin unauthenticated.
    assert plugin_services(env) == {"whatsapp": ("http://plugin-whatsapp:8080", "t1")}


def test_discovery_registers_and_creates_a_disabled_row(vendored_echo, echo_impl, tmp_path):
    register_remote(echo_impl, tmp_path)
    reg = get_registry()
    assert reg.service_of("echo") == "echosvc"
    assert reg.plugin_rows()["echo"]["enabled"] == 0
    assert reg.enabled_plugins() == []
    assert [(m.id, e, c) for m, e, c in reg.plugin_states()] == [("echo", False, False)]


def test_discovery_via_env(vendored_echo, echo_impl, tmp_path, monkeypatch):
    runtime = serve([echo_impl], PLUGIN_TOKEN, tmp_path, Fernet.generate_key().decode())
    monkeypatch.setenv("PLUGIN_URL_ECHOSVC", "http://plugin-echo:8080")
    monkeypatch.setenv("PLUGIN_TOKEN_ECHOSVC", PLUGIN_TOKEN)
    get_registry().discover(client_factory=runtime_factory(runtime))
    assert "echo" in get_registry().entries()


def test_version_mismatch_is_refused_and_audited(vendored_echo, echo_impl, tmp_path):
    echo_impl.manifest = {**echo_impl.manifest, "version": "9.9.9"}
    runtime = serve([echo_impl], PLUGIN_TOKEN, tmp_path, Fernet.generate_key().decode())
    get_registry().discover({"echosvc": ("http://x", PLUGIN_TOKEN)},
                            client_factory=runtime_factory(runtime))
    assert "echo" not in get_registry().entries()
    assert "mismatch" in get_registry().refused["echo"]
    [row] = _audit("plugin.refused")
    assert row["resource"] == "echo" and row["result"] == "denied"


def test_unvendored_plugin_is_refused(vendored_echo, echo_impl, tmp_path):
    echo_impl.manifest = {**echo_impl.manifest, "id": "rogue"}
    runtime = serve([echo_impl], PLUGIN_TOKEN, tmp_path, Fernet.generate_key().decode())
    get_registry().discover({"s": ("http://x", PLUGIN_TOKEN)},
                            client_factory=runtime_factory(runtime))
    assert get_registry().entries() == {}
    assert "no vendored manifest" in get_registry().refused["rogue"]


def test_plugin_cannot_widen_its_lattice(vendored_echo, echo_impl, tmp_path):
    # Same id and version, but an extra action and no narrowings: the
    # vendored manifest is what the broker uses.
    m = dict(echo_impl.manifest)
    m["actions"] = [*m["actions"], {"name": "drop_everything", "side_effect": "destructive"}]
    m["narrowings"] = []
    echo_impl.manifest = m
    register_remote(echo_impl, tmp_path)
    pinned = get_registry().manifests()["echo"]
    assert "drop_everything" not in pinned.action_names
    assert pinned.model_dump() == echo_manifest().model_dump()


def test_in_process_registration_is_pinned_too(env, echo_impl):
    echo_impl.manifest = {**echo_impl.manifest, "version": "0.0.1"}
    assert get_registry().register_in_process(echo_impl, echo_manifest()) is False
    assert get_registry().entries() == {}


def test_duplicate_id_from_a_second_service_is_refused(vendored_echo, echo_impl, tmp_path):
    register_remote(echo_impl, tmp_path)
    runtime = serve([echo_impl], PLUGIN_TOKEN, tmp_path / "b", Fernet.generate_key().decode())
    get_registry()._factory = runtime_factory(runtime)
    get_registry()._discover_one("othersvc", "http://y", PLUGIN_TOKEN)
    assert get_registry().service_of("echo") == "echosvc"
    assert "already served" in get_registry().refused["echo"]


def test_unreachable_service_is_retried_later(vendored_echo, echo_impl, tmp_path):
    runtime = serve([echo_impl], PLUGIN_TOKEN, tmp_path, Fernet.generate_key().decode())
    working = runtime_factory(runtime)
    state = {"up": False}

    def flaky(base_url, headers, timeout):
        if not state["up"]:
            raise httpx.ConnectError("connection refused")
        return working(base_url, headers, timeout)

    reg = get_registry()
    reg.discover({"echosvc": ("http://x", PLUGIN_TOKEN)}, client_factory=flaky)
    assert reg.entries() == {} and "echosvc" in reg._pending
    state["up"] = True
    reg._next_discovery = 0                    # the retry interval has passed
    assert "echo" in reg.entries()


def test_enable_disable_flips_states(vendored_echo, echo_impl, tmp_path):
    register_remote(echo_impl, tmp_path)
    reg = get_registry()
    enable_plugin()
    assert reg.enabled_plugins() == ["echo"]
    assert [(e, c) for _, e, c in reg.plugin_states()] == [(True, True)]
    settings.set_enabled("echo", False)
    assert reg.enabled_plugins() == [] and reg.enabled_manifests() == {}


def test_registry_is_a_singleton_and_builds_the_lattice(echo_local):
    reg = get_registry()
    assert reg is get_registry()
    lat = reg.lattice()
    assert "echo" in lat.forms and lat.forms["echo"]["folder"].form == "subtree"
    assert tuple(lat.ancestors("folder", "a1x")) == ("a1", "a", "root")


def test_ancestors_are_cached_and_failures_are_not(echo_local, monkeypatch):
    reg = get_registry()
    calls = []
    real = echo_local.impl.ancestors

    def counting(kind, rid):
        calls.append(rid)
        return real(kind, rid)

    monkeypatch.setattr(echo_local.impl, "ancestors", counting)
    reg.ancestors("folder", "a1")
    reg.ancestors("folder", "a1")
    assert calls == ["a1"]

    def broken(kind, rid):
        raise AdapterError(503, "down")
    monkeypatch.setattr(echo_local.impl, "ancestors", broken)
    assert reg.ancestors("folder", "b1") == ()          # fail closed: no ancestry
    monkeypatch.setattr(echo_local.impl, "ancestors", counting)
    assert reg.ancestors("folder", "b1") == ("b", "root")


def test_ancestry_for_unowned_kind_is_empty(echo_local):
    # `room` is a list dimension: nobody answers ancestry for it.
    assert get_registry().ancestors("room", "r1") == ()


def test_ambiguous_kind_has_no_ancestry(echo_local):
    # A second plugin declaring the same subtree kind: neither may answer.
    reg = get_registry()
    reg._entries["echo2"] = registry.Entry(reg._entries["echo"].manifest,
                                           reg._entries["echo"].adapter, "x")
    assert reg.ancestors("folder", "a1x") == ()


# ------------------------------------------------------------ RemoteAdapter contract

def _mock(handler):
    def factory(base_url, headers, timeout):
        return httpx.Client(base_url=base_url, headers=headers,
                            transport=httpx.MockTransport(handler))
    return factory


def _remote(handler):
    return RemoteAdapter("svc", "http://plugin", "tok-SECRET-123", echo_manifest(),
                         client_factory=_mock(handler))


@pytest.mark.parametrize("exc,status", [(httpx.ConnectError("refused"), 503),
                                        (httpx.ConnectTimeout("slow"), 503),
                                        (httpx.ReadTimeout("after send"), 502),
                                        (httpx.RemoteProtocolError("reset"), 502)])
def test_transport_failures_map_to_503_or_502(exc, status):
    def handler(request):
        raise exc
    with pytest.raises(AdapterError) as e:
        _remote(handler).perform("post_item", {}, CallScope("r"))
    assert e.value.status == status
    assert "tok-SECRET-123" not in e.value.message


@pytest.mark.parametrize("upstream,mapped", [(400, 400), (404, 404), (409, 409), (503, 503),
                                             (502, 502), (500, 502), (504, 502)])
def test_upstream_status_passthrough(upstream, mapped):
    r = _remote(lambda req: httpx.Response(upstream, json={"error": "nope"}))
    with pytest.raises(AdapterError) as e:
        r.perform("post_item", {}, CallScope("r"))
    assert e.value.status == mapped and e.value.message == "nope"


def test_headers_carry_token_plugin_id_and_request_id():
    seen = {}

    def handler(req):
        seen.update(req.headers)
        return httpx.Response(200, json={"data": {"ok": True}})

    out = _remote(handler).perform("list_items", {}, CallScope("rid-1"))
    assert out.data == {"ok": True}
    assert seen["x-plugin-token"] == "tok-SECRET-123"
    assert seen["x-plugin-id"] == "echo" and seen["x-request-id"] == "rid-1"


def test_malformed_responses_are_unknown_outcomes():
    for resp in (httpx.Response(200, text="not json"), httpx.Response(200, json=[1, 2]),
                 httpx.Response(200, json={"binary_b64": "!!!"})):
        with pytest.raises(AdapterError) as e:
            _remote(lambda req, r=resp: r).perform("get_blob", {}, CallScope("r"))
        assert e.value.status == 502


def test_repr_never_shows_the_token():
    assert "tok-SECRET-123" not in repr(_remote(lambda r: httpx.Response(200, json={})))


def test_request_helper_uses_the_timeout():
    seen = {}

    def factory(base_url, headers, timeout):
        seen["timeout"] = timeout
        return httpx.Client(base_url=base_url, transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"manifests": []})))
    request("http://x", "t", "GET", "/manifests", timeout=7.5, factory=factory)
    assert seen["timeout"] == 7.5
