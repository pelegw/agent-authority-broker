"""Plugin registry: env discovery, manifest pinning (in-tree files first,
then the owner's database pins), refused offers kept for review and re-pinned,
plugins rows, states, the lattice singleton, and the RemoteAdapter error
contract."""

import httpx
import pytest
from cryptography.fernet import Fernet

from aab_plugin_runtime import serve
from broker import db
from broker.config import plugin_services
from broker.plugins import pins, registry, settings
from broker.plugins.adapter import AdapterError, CallScope, RemoteAdapter, request
from broker.plugins.registry import get_registry

from .conftest import (ECHO_DIR, PLUGIN_TOKEN, echo_manifest, enable_plugin,
                       register_remote, runtime_factory)


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


# ------------------------------------------------------------ database pins and offers

ECHO_TEXT = (ECHO_DIR / "manifest.yaml").read_text(encoding="utf-8")


@pytest.fixture()
def external(env, tmp_path):
    """A registry with nothing vendored in its tree: echo is an external
    plugin, pinned (or not) only in the database."""
    registry.reset_registry(registry.Registry(vendored_dirs=(tmp_path / "no-tree",)))


def _offer(echo_impl, tmp_path, service="echosvc"):
    runtime = serve([echo_impl], PLUGIN_TOKEN, tmp_path / f"{service}-secrets",
                    Fernet.generate_key().decode())
    get_registry().discover({service: ("http://plugin-echo", PLUGIN_TOKEN)},
                            client_factory=runtime_factory(runtime))
    return runtime


def _versioned(version: str) -> str:
    return ECHO_TEXT.replace("version: 0.1.0", f"version: {version}")


def test_a_database_pin_is_accepted(external, echo_impl, tmp_path):
    pins.set("echo", ECHO_TEXT, by="owner")
    register_remote(echo_impl, tmp_path)
    reg = get_registry()
    assert reg.manifests()["echo"].model_dump() == echo_manifest().model_dump()
    assert reg.offered == {} and "echo" not in reg.refused
    assert reg.adapter("echo").status()["healthy"] is True       # the adapter works


def test_the_database_pin_is_what_is_used_not_the_offer(external, echo_impl, tmp_path):
    # Same id and version, wider offer: the stored copy wins, as in-tree.
    pins.set("echo", ECHO_TEXT, by="owner")
    echo_impl.manifest = {**echo_impl.manifest, "actions": [
        *echo_impl.manifest["actions"], {"name": "drop_everything", "side_effect": "destructive"}]}
    register_remote(echo_impl, tmp_path)
    assert "drop_everything" not in get_registry().manifests()["echo"].action_names


def test_in_tree_beats_the_database(vendored_echo, echo_impl, tmp_path):
    pins.set("echo", _versioned("9.9.9"), by="owner")
    reg = get_registry()
    assert reg.vendored("echo").version == "0.1.0"
    register_remote(echo_impl, tmp_path)                 # offers 0.1.0: the tree's version
    assert reg.manifests()["echo"].version == "0.1.0"
    assert reg.offered == {}


def test_a_mismatch_against_a_database_pin_is_refused_and_offered(external, echo_impl,
                                                                  tmp_path):
    pins.set("echo", _versioned("0.0.9"), by="owner")
    _offer(echo_impl, tmp_path)
    reg = get_registry()
    assert "echo" not in reg.entries()
    assert "mismatch" in reg.refused["echo"]
    offer = reg.offered["echo"]
    assert offer["service"] == "echosvc" and "mismatch" in offer["reason"]
    assert offer["manifest"]["version"] == "0.1.0"        # what the service offered
    [row] = _audit("plugin.refused")
    assert row["resource"] == "echo" and row["result"] == "denied"


def test_an_unpinned_offer_is_kept_for_review(external, echo_impl, tmp_path):
    _offer(echo_impl, tmp_path)
    reg = get_registry()
    assert reg.entries() == {}
    assert "no vendored manifest" in reg.offered["echo"]["reason"]
    assert reg.offers() == reg.offered and reg.offers() is not reg.offered


def test_repin_registers_the_offer_and_clears_it(external, echo_impl, tmp_path):
    _offer(echo_impl, tmp_path)
    reg = get_registry()
    assert reg.repin("echo") is False                    # nothing pinned yet: still refused
    assert "echo" in reg.offered
    pins.set("echo", ECHO_TEXT, by="owner")
    assert reg.repin("echo") is True
    assert reg.service_of("echo") == "echosvc"
    assert reg.offered == {} and "echo" not in reg.refused
    assert reg.adapter("echo").status()["healthy"] is True
    assert reg.plugin_rows()["echo"]["enabled"] == 0     # a new plugin starts disabled
    assert reg.repin("echo") is False                    # nothing left to re-pin


def test_repin_of_an_in_process_offer(external, echo_impl):
    from broker.plugins.adapter import InProcessAdapter
    reg = get_registry()
    assert reg.register(InProcessAdapter(echo_impl, echo_manifest()), echo_impl.manifest) is False
    pins.set("echo", ECHO_TEXT, by="owner")
    assert reg.repin("echo") is True and "echo" in reg.entries()


def test_withdraw_stops_serving_and_offers_again(external, echo_impl, tmp_path):
    pins.set("echo", ECHO_TEXT, by="owner")
    register_remote(echo_impl, tmp_path)
    reg = get_registry()
    assert reg.withdraw("echo", "pin removed") is True
    assert "echo" not in reg.entries()
    assert reg.offered["echo"]["reason"] == "pin removed"
    assert reg.offered["echo"]["manifest"]["id"] == "echo"
    assert reg.withdraw("echo", "again") is False
    assert reg.repin("echo") is True                     # the pin still exists


def test_clear_cache_resets_offers_but_hiding_does_not(external, echo_impl, tmp_path):
    _offer(echo_impl, tmp_path)
    reg = get_registry()
    reg.clear_ancestry_cache()                           # what hiding a resource calls
    assert "echo" in reg.offered
    reg.clear_cache()
    assert reg.offered == {} and reg.repin("echo") is False


def test_rediscovery_rerecords_offers(external, echo_impl, tmp_path):
    _offer(echo_impl, tmp_path)
    reg = get_registry()
    reg.offered["ghost"] = {"manifest": {}, "service": "echosvc", "reason": "stale"}
    _offer(echo_impl, tmp_path)
    assert set(reg.offered) == {"echo"}


def test_an_unreadable_database_pin_is_refused_not_fatal(external, echo_impl, tmp_path):
    with db.connect() as conn:
        conn.execute("INSERT INTO plugin_pins (plugin_id, version, manifest_yaml, pinned_at,"
                     " pinned_by) VALUES ('echo', '0.1.0', 'id: [unclosed', 0, 'owner')")
    _offer(echo_impl, tmp_path)
    reg = get_registry()
    assert reg.entries() == {}
    assert "no vendored manifest" in reg.refused["echo"]


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


# ------------------------------------------------------------ dynamic services (the installer's)

TOKEN_A = "a1" * 32
TOKEN_B = "b2" * 32


def _dyn(service="echo", token=TOKEN_A):
    return {service: (registry.dynamic_url(service), token)}


def test_reserved_services_match_the_installers():
    """The registry refuses exactly the names the installer refuses."""
    import sys
    from pathlib import Path
    try:
        from aab_installer.descriptor import RESERVED_SERVICES
    except ImportError:
        sys.path.append(str(Path(__file__).resolve().parents[2] / "installer"))
        from aab_installer.descriptor import RESERVED_SERVICES
    assert registry.RESERVED_SERVICES == RESERVED_SERVICES


@pytest.mark.parametrize("mapping,fragment", [
    ([("echo", "x")], "not a mapping"),
    ({"Echo": ("http://plugin-Echo:8090", TOKEN_A)}, "malformed"),
    ({"e": ("http://plugin-e:8090", TOKEN_A)}, "malformed"),
    ({"whatsapp": ("http://plugin-whatsapp:8090", TOKEN_A)}, "stack itself uses"),
    ({"echo": ["http://plugin-echo:8090", TOKEN_A]}, "malformed entry"),
    ({"echo": ("http://plugin-echo:8091", TOKEN_A)}, "the url is not"),
    ({"echo": ("http://" + TOKEN_B + "@plugin-echo:8090", TOKEN_A)}, "the url is not"),
    ({"echo": ("http://plugin-echo:8090", "short")}, "malformed token"),
    ({"echo": ("http://plugin-echo:8090", TOKEN_A + " x")}, "malformed token"),
    ({"echo": ("http://plugin-echo:8090", None)}, "malformed token"),
    ({f"s{i:02d}": (registry.dynamic_url(f"s{i:02d}"), TOKEN_A) for i in range(65)},
     "more than 64"),
])
def test_dynamic_services_are_validated_whole(env, mapping, fragment):
    reg = get_registry()
    reg.set_dynamic_services(_dyn())
    with pytest.raises(ValueError, match=fragment) as e:
        reg.set_dynamic_services(mapping)
    # Nothing changed, and the error names no URL and no token.
    assert reg.dynamic_services() == ["echo"]
    assert TOKEN_A not in str(e.value) and TOKEN_B not in str(e.value)


def test_dynamic_services_merge_with_env(env, monkeypatch):
    monkeypatch.setenv("PLUGIN_URL_ENVSVC", "http://plugin-envsvc:8090")
    monkeypatch.setenv("PLUGIN_TOKEN_ENVSVC", "env-token")
    reg = get_registry()
    change = reg.set_dynamic_services(_dyn())
    assert change.fresh == _dyn() and change.removed == ()
    assert TOKEN_A not in repr(change)                            # fresh holds tokens
    assert reg.services() == {"echo": _dyn()["echo"],
                              "envsvc": ("http://plugin-envsvc:8090", "env-token")}
    # Removing every dynamic service leaves the env one alone.
    change = reg.set_dynamic_services({})
    assert change.removed == ("echo",) and change.fresh == {}
    assert list(reg.services()) == ["envsvc"]


def test_the_installers_values_win_for_a_service_it_lists(env, monkeypatch):
    monkeypatch.setenv("PLUGIN_URL_ECHO", registry.dynamic_url("echo"))
    monkeypatch.setenv("PLUGIN_TOKEN_ECHO", TOKEN_A)
    reg = get_registry()
    assert reg.services()["echo"][1] == TOKEN_A                  # env alone
    change = reg.set_dynamic_services(_dyn(token=TOKEN_B))
    assert change.fresh == _dyn(token=TOKEN_B)                   # reached differently now
    assert reg.services()["echo"][1] == TOKEN_B
    # The same values from both sources: nothing to discover again.
    reg.set_dynamic_services({})
    assert "echo" not in reg.services()        # listed before, not now: gone, env or not
    reg2 = registry.reset_registry()
    assert reg2.set_dynamic_services(_dyn(token=TOKEN_A)).fresh == {}


def test_an_env_service_the_installer_never_listed_is_never_evicted(env, monkeypatch):
    monkeypatch.setenv("PLUGIN_URL_ECHOSVC", "http://plugin-echo")
    monkeypatch.setenv("PLUGIN_TOKEN_ECHOSVC", PLUGIN_TOKEN)
    reg = get_registry()
    reg.set_dynamic_services(_dyn("other"))
    reg.set_dynamic_services({})
    assert "echosvc" in reg.services() and "other" not in reg.services()


def test_eviction_unregisters_at_once_and_forgets_offers_and_retries(external, echo_impl,
                                                                     tmp_path):
    pins.set("echo", ECHO_TEXT, by="owner")
    runtime = serve([echo_impl], TOKEN_A, tmp_path / "s", Fernet.generate_key().decode())
    reg = get_registry()
    reg.discover({}, client_factory=runtime_factory(runtime))
    reg.discover(reg.set_dynamic_services(_dyn()).fresh)
    assert reg.service_of("echo") == "echo"
    enable_plugin()
    reg.offered["ghost"] = {"manifest": {}, "service": "echo", "reason": "stale"}
    reg._pending["echo"] = _dyn()["echo"]
    change = reg.set_dynamic_services({})
    assert change.removed == ("echo",)
    assert "echo" not in reg.entries() and reg.offered == {} and reg.pending_services() == []
    assert reg.plugin_rows()["echo"]["enabled"] == 1             # the row is untouched


def test_a_partial_discovery_keeps_other_services_pending(vendored_echo, echo_impl, tmp_path):
    runtime = serve([echo_impl], PLUGIN_TOKEN, tmp_path, Fernet.generate_key().decode())
    working = runtime_factory(runtime)

    def factory(base_url, headers, timeout):
        if "down" in base_url:
            raise httpx.ConnectError("refused")
        return working(base_url, headers, timeout)

    reg = get_registry()
    reg.discover({"downsvc": ("http://down", PLUGIN_TOKEN)}, client_factory=factory)
    assert reg.pending_services() == ["downsvc"]
    reg.discover({"echosvc": ("http://up", PLUGIN_TOKEN)})       # e.g. a just-installed one
    assert reg.pending_services() == ["downsvc"] and "echo" in reg.entries()


def test_a_pending_service_is_retried_with_its_current_token(external, echo_impl, tmp_path):
    pins.set("echo", ECHO_TEXT, by="owner")
    runtime = serve([echo_impl], TOKEN_B, tmp_path / "s", Fernet.generate_key().decode())
    reg = get_registry()
    reg.discover({}, client_factory=runtime_factory(runtime))
    reg.discover(reg.set_dynamic_services(_dyn(token=TOKEN_A)).fresh)  # 401: wrong token
    assert reg.pending_services() == ["echo"]
    reg._dynamic = dict(_dyn(token=TOKEN_B))     # the list changed; no discovery yet
    reg._next_discovery = 0
    assert reg.service_of("echo") == "echo"      # the lazy retry used TOKEN_B


def test_a_service_that_stops_offering_a_plugin_stops_serving_it(external, echo_impl,
                                                                 tmp_path):
    pins.set("echo", ECHO_TEXT, by="owner")
    register_remote(echo_impl, tmp_path)
    reg = get_registry()
    assert "echo" in reg.entries()
    # The service's new container answers, offering nothing (or garbage).
    for body in ({"manifests": []}, {"manifests": "nope"}):
        register_remote(echo_impl, tmp_path)
        assert "echo" in reg.entries()
        reg.discover({"echosvc": ("http://plugin-echo", PLUGIN_TOKEN)},
                     client_factory=_mock(lambda r, b=body: httpx.Response(200, json=b)))
        assert "echo" not in reg.entries()
