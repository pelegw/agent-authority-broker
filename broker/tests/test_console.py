"""The owner console page: how it is served, and what the file may contain.

The page is one static file whose JavaScript cannot run under pytest, so
these tests hold it to structural rules instead: strict headers with a
per-request script nonce, no external resources, no HTML-string sinks or
inline handlers (agent-written text must never become markup), every API
path it calls exists, and every manifest vocabulary word it must render
(config field types, narrowing forms, connection kinds, roles) is covered.
Plus the manifest projection the console's editors are generated from.
"""

import re
import typing
from pathlib import Path

import pytest

from broker import deps
from broker.authority import roles
from broker.plugins import manifest as manifest_mod
from broker.plugins.manifest import ConfigField, Connection, load_manifest

from .conftest import ECHO_DIR

PAGE = Path(__file__).resolve().parents[1] / "broker" / "templates" / "console.html"
GITHUB = Path(__file__).resolve().parents[1] / "broker" / "targets" / "github" / "manifest.yaml"
NONCE_RE = re.compile(r"script-src 'nonce-([A-Za-z0-9_-]{16,})'")


@pytest.fixture(scope="module")
def html() -> str:
    return PAGE.read_text(encoding="utf-8")


def _script(html: str) -> str:
    scripts = re.findall(r"<script\b[^>]*>(.*?)</script>", html, re.S)
    assert len(scripts) == 1, "the console has exactly one script"
    return scripts[0]


def _markup(html: str) -> str:
    """The page with its script and comments removed: static markup only."""
    out = re.sub(r"<script\b[^>]*>.*?</script>", "", html, flags=re.S)
    return re.sub(r"<!--.*?-->", "", out, flags=re.S)


def _block(html: str, name: str) -> str:
    m = re.search(rf"/\* {name}:start \*/(.*?)/\* {name}:end \*/", html, re.S)
    assert m, f"marker block {name} missing"
    return m.group(1)


def _js_list(html: str, const: str) -> list[str]:
    m = re.search(rf"const {const} = \[(.*?)\];", html, re.S)
    assert m, f"const {const} missing"
    return re.findall(r'"([^"]*)"', m.group(1))


def _object_keys(block: str) -> set[str]:
    """Top-level keys of a JS object literal written one entry per line as
    `name(...) {`, `name: ...` or `name,` (the marker blocks use these)."""
    body = block[block.index("{") + 1:]
    return set(re.findall(r"^[ ]{2}([a-z_]+)\s*(?:\(|:|,)", body, re.M))


# ---- serving -------------------------------------------------------------------

def test_admin_serves_the_page_with_strict_headers(client):
    r = client.get("/admin")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "no-referrer"
    csp = dict(part.strip().split(" ", 1) for part in r.headers["content-security-policy"].split(";"))
    assert csp["default-src"] == "'self'"
    assert NONCE_RE.fullmatch("script-src " + csp["script-src"])
    # The whole point of the nonce: no inline script without it.
    assert "unsafe-inline" not in csp["script-src"] and "unsafe-eval" not in csp["script-src"]
    assert csp["style-src"] == "'self' 'unsafe-inline'"
    assert csp["img-src"] == "'self' data:"
    assert csp["connect-src"] == "'self'"
    assert csp["frame-ancestors"] == "'none'"
    assert csp["base-uri"] == "'none'"
    assert csp["form-action"] == "'none'"
    assert csp["object-src"] == "'none'"


def test_nonce_is_fresh_per_request_and_marks_the_only_script(client):
    first, second = client.get("/admin"), client.get("/admin")
    n1 = NONCE_RE.search(first.headers["content-security-policy"]).group(1)
    n2 = NONCE_RE.search(second.headers["content-security-policy"]).group(1)
    assert n1 != n2
    assert "{{NONCE}}" not in first.text
    tags = re.findall(r"<script\b[^>]*>", first.text)
    assert tags == [f'<script nonce="{n1}">']


def test_template_has_exactly_one_nonce_slot(html):
    assert html.count("{{NONCE}}") == 1
    assert '<script nonce="{{NONCE}}">' in html


def test_every_admin_path_serves_the_same_page(client):
    def body(path):
        r = client.get(path)
        assert r.status_code == 200, path
        assert r.headers["content-security-policy"].startswith("default-src 'self'")
        return re.sub(r'nonce="[^"]+"', "", r.text)

    base = body("/admin")
    for path in ("/admin/", "/admin/keys", "/admin/anything/deeper/still", "/admin/..%2F..%2Fetc"):
        assert body(path) == base, path


def test_page_needs_no_owner_credential_and_carries_no_data(client):
    """Fetching the page is allowed before setup and without a session, and
    it is byte-for-byte the same page afterwards: it holds no data at all."""
    from broker import auth
    from broker.identity import principals

    def page():
        r = client.get("/admin")
        assert r.status_code == 200
        assert "set-cookie" not in r.headers
        return re.sub(r'nonce="[^"]+"', "", r.text)

    before = page()
    owner = principals.create_owner("distinctowner", "a long enough password")
    auth.create_key(owner.id, "distinct-agent-key", "read-only", 6, None)
    after = page()
    assert after == before
    assert "distinctowner" not in after and "distinct-agent-key" not in after


def test_page_needs_cloudflare_access_when_it_is_on(client, monkeypatch):
    # Access enabled but unconfigured rejects every assertion: enough to show
    # the page sits behind require_cf_access like /auth/*.
    from broker.config import get_settings
    monkeypatch.setenv("CF_ACCESS_ENABLED", "true")
    get_settings.cache_clear()
    assert client.get("/admin").status_code == 403
    assert client.get("/admin/keys").status_code == 403


def test_page_routes_stay_out_of_the_api_schema(env):
    from broker.main import api
    assert not [p for p in api.openapi()["paths"] if p == "/admin" or p.startswith("/admin/")]


# ---- what the file may contain -------------------------------------------------

def _strip_comments(text: str) -> str:
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    # Whole-line // comments only: a trailing-comment regex would also eat
    # the "//host" half of any URL and hide it from the check below.
    return re.sub(r"^\s*//.*$", "", text, flags=re.M)


def test_no_external_resources(html):
    code = _strip_comments(html)
    assert not re.search(r"https?://", code, re.I)
    assert not re.search(r"""(?:src|href|action)\s*=\s*["']?\s*//""", code, re.I)
    style = re.search(r"<style>(.*?)</style>", html, re.S).group(1)
    assert "@import" not in code and "url(" not in style
    # And nothing even in comments points outside the broker.
    assert not re.search(r"https?://", html, re.I)


def test_no_html_string_sinks(html):
    """Agents write text this page shows to the person approving them. With
    no API that parses strings as HTML, that text can only ever be text."""
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write",
                 "createContextualFragment", "DOMParser", "srcdoc", "eval(",
                 "new Function", "javascript:", "setTimeout(\"", "setInterval(\""):
        assert sink not in html, sink
    assert not re.search(r"""setAttribute\(\s*["'`]on""", html)


def test_source_has_no_invisible_bidi_controls(html):
    """Trojan-source guard: the page's own code never hides bidi controls."""
    bad = [hex(ord(c)) for c in html
           if 0x202A <= ord(c) <= 0x202E or 0x2066 <= ord(c) <= 0x2069 or ord(c) in (0x200E, 0x200F)]
    assert not bad, bad


def test_every_text_node_shows_bidi_controls(html):
    # h() is the only place script creates text nodes, and it marks
    # override/embedding/isolate controls visibly (an agent could otherwise
    # reorder a summary or a file name on the approval card).
    script = _script(html)
    assert script.count("createTextNode(") == 1
    assert "createTextNode(showControls(" in script
    assert r"const BIDI_CONTROLS = /[\u202A-\u202E\u2066-\u2069]/g;" in script


def test_no_inline_event_handlers_in_markup(html):
    # The nonce CSP would refuse them anyway; this keeps the page working.
    assert not re.search(r"<[^>]*\son[a-z]+\s*=", _markup(html), re.I)


def test_csrf_header_matches_deps(html):
    script = _script(html)
    header = re.search(r'const CSRF_HEADER = "([^"]+)";', script).group(1)
    value = re.search(r'const CSRF_VALUE = "([^"]+)";', script).group(1)
    assert header.lower() == deps.CSRF_HEADER.lower()
    assert value == deps.CSRF_VALUE


def test_roles_match_the_role_ladder(html):
    assert _js_list(html, "ROLES") == list(roles.ROLES)


def test_narrowing_forms_match_the_manifest_vocabulary(html):
    assert set(_js_list(html, "SET_FORMS")) == set(manifest_mod.SET_FORMS)
    assert set(_js_list(html, "SCALAR_FORMS")) == set(manifest_mod.SCALAR_FORMS)
    assert _object_keys(_block(html, "scalar-inputs")) == set(manifest_mod.SCALAR_FORMS)
    for form in manifest_mod.SET_FORMS:
        assert re.search(rf"\b{form}: \"", _script(html)), f"FORM_TEXT lacks {form}"
    assert _js_list(html, "EFFECTS") == list(manifest_mod.SIDE_EFFECTS)


def test_config_form_covers_every_manifest_field_type(html):
    allowed = set(typing.get_args(ConfigField.model_fields["type"].annotation))
    assert allowed == {"string", "text", "integer", "boolean", "enum"}
    assert _object_keys(_block(html, "config-renderers")) == allowed


def test_connection_panel_covers_every_connection_kind(html):
    kinds = set(typing.get_args(Connection.model_fields["kind"].annotation))
    assert _object_keys(_block(html, "connectors")) == kinds


def test_views_and_nav_agree_and_pass2_placeholders_exist(html):
    sections = re.findall(r'<section class="view" data-view="([a-z]+)"', html)
    nav = re.findall(r'<a href="#/([a-z]+)" data-view="\1">', html)
    assert sorted(sections) == sorted(nav) == sorted(_js_list(html, "VIEWS"))
    for view in ("channels", "settings", "delegations"):
        assert view in sections
        sec = re.search(rf'<section class="view" data-view="{view}".*?</section>', html, re.S).group(0)
        assert "Available after the next merge." in sec


# ---- every API path the page calls exists ---------------------------------------

_TEMPLATE_EXPR = re.compile(r"\$\{[^{}]*\}")
_API_PATH = re.compile(r"(?<![\w/.])/(?:v1|auth)/[A-Za-z0-9_\-./{}]*")


def page_api_paths(html: str) -> set[str]:
    """Every /v1/... and /auth/... literal, with ${...} holes as {x}."""
    text = _TEMPLATE_EXPR.sub("{x}", html)
    return {m.group(0).rstrip("/.") for m in _API_PATH.finditer(text)}


def _segments_match(page: str, served: str) -> bool:
    a, b = page.strip("/").split("/"), served.strip("/").split("/")
    return len(a) == len(b) and all(
        x == y or x.startswith("{") or y.startswith("{") for x, y in zip(a, b))


def test_every_api_path_in_the_page_exists(env, html):
    from broker.main import api
    served = list(api.openapi()["paths"])
    used = page_api_paths(html)
    # Not vacuous: the views' main calls are all found.
    for must in ("/auth/status", "/auth/login", "/auth/setup", "/auth/logout", "/auth/me",
                 "/v1/admin/plugins", "/v1/admin/keys", "/v1/admin/grants",
                 "/v1/admin/actions", "/v1/admin/hidden", "/v1/admin/resolve",
                 "/v1/admin/decisions", "/v1/admin/decisions/verify", "/v1/admin/tokens",
                 "/v1/admin/sessions", "/v1/admin/password",
                 "/v1/admin/plugins/{x}/connect/qr.png"):
        assert must in used, must
    missing = sorted(p for p in used if not any(_segments_match(p, s) for s in served))
    assert not missing, missing


def test_the_path_extractor_catches_a_bad_path():
    fake = 'api(`/v1/admin/nonexistent/${enc(id)}/thing`); api("/auth/nope?x=1")'
    assert page_api_paths(fake) == {"/v1/admin/nonexistent/{x}/thing", "/auth/nope"}


# ---- the manifest projection the console is generated from -----------------------

def test_plugin_view_carries_the_console_projection(client, admin_headers, echo_local):
    item = next(p for p in client.get("/v1/admin/plugins", headers=admin_headers).json()["items"]
                if p["id"] == "echo")
    m = item["manifest"]
    assert [s["name"] for s in m["selectors"]] == ["room", "folder", "sender"]
    assert {s["name"]: s["form"] for s in m["selectors"]} == {
        "room": "list", "folder": "subtree", "sender": "pattern"}
    # Scalar forms are constraints wherever the manifest declared them: the
    # `visibility` level is a narrowing in the manifest, a constraint in a cap.
    cons = {c["name"]: c for c in m["constraints"]}
    assert set(cons) == {"visibility", "window_days", "attachments"}
    assert cons["visibility"]["values"] == ["summary", "full"]
    assert cons["window_days"]["default"] == 30
    acts = {a["name"]: a for a in m["actions"]}
    assert acts["post_item"]["side_effect"] == "write"
    assert acts["post_item"]["modes"] == ["direct", "draft"]
    assert acts["post_item"]["summary_template"] == "Post to {room_label}: {text}"
    assert acts["touch_item"]["modes"] == ["direct"]
    assert acts["list_items"]["modes"] == ["direct"]
    assert m["resources"]["room"] == {"display": "Room", "resolve": True, "hideable": True,
                                      "id_format": "room id, e.g. r1"}


def test_projection_matches_the_lattice_split_and_drops_derived_dimensions():
    from broker.authority.capability import target_forms
    from broker.plugins.manifest_view import admin_view
    for m in (load_manifest(ECHO_DIR / "manifest.yaml"), load_manifest(GITHUB)):
        view = admin_view(m)
        forms = target_forms(m)
        assert {s["name"] for s in view["selectors"]} == {
            n for n, f in forms.items() if f.form in manifest_mod.SET_FORMS}
        assert {c["name"] for c in view["constraints"]} == {
            n for n, f in forms.items() if f.form in manifest_mod.SCALAR_FORMS}
    github = admin_view(load_manifest(GITHUB))
    assert "permissions" not in {c["name"] for c in github["constraints"]}   # derived
