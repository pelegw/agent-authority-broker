"""The owner console page: how it is served, and what the file may contain.

The page is one static file whose JavaScript cannot run under pytest, so
these tests hold it to structural rules instead: strict headers with a
per-request script nonce, no external resources, no HTML-string sinks or
inline handlers (agent-written text must never become markup), every API
path it calls exists, every vocabulary word it must render (config field
types, narrowing forms, connection kinds, roles, setting types) is covered,
the key dialogs present the role as a ceiling (label, help line, default
`full`, and an effective-mode table equal to roles.role_caps), the
Telegram bot token field is write-only, and the Plugins view's + Add plugin
flow (installer off or on, the review card rendered from the broker's real
review, a job panel that polls through the broker's restart, the offered
card, the write-only GitHub token for private repositories). Where node
exists, the whole script must parse. Plus the manifest projection the
console's editors are generated from.
"""

import json
import re
import shutil
import subprocess
import typing
from pathlib import Path

import pytest

from broker import auth, deps, runtime_settings
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


# ---- the role as a ceiling -------------------------------------------------------

def _ceiling_table(html: str) -> dict[str, dict[str, str]]:
    m = re.search(r"const CEILING_MODES = \{(.*?)\n\};", _script(html), re.S)
    assert m, "the effective-mode table CEILING_MODES is missing"
    return {role: dict(re.findall(r'(\w+): "([a-z]*)"', body))
            for role, body in re.findall(r'"([a-z-]+)": \{([^}]*)\}', m.group(1))}


def test_effective_mode_table_matches_the_role_caps(html):
    """CEILING_MODES is what the capability editor's per-action badges are
    computed from: per ceiling and side effect, the highest mode a call can
    run at ("" = never). It must be exactly roles.role_caps, or the badge
    would say "direct" where the broker drafts."""
    echo = load_manifest(ECHO_DIR / "manifest.yaml")
    one_of = {eff: next(a.name for a in echo.actions if a.side_effect == eff)
              for eff in manifest_mod.SIDE_EFFECTS}
    expected = {}
    for role in roles.ROLES:
        caps = roles.role_caps(echo, role)
        expected[role] = {eff: next((c.mode for c in caps if name in c.actions), "")
                          for eff, name in one_of.items()}
    assert _ceiling_table(html) == expected


def test_effective_mode_follows_the_brokers_rule(html):
    """The JS mirror of role_ceiling.mode_under: the ceiling's bound from the
    table (unknown role or side effect: nothing), the lower of the two modes,
    then run_mode (an action that cannot run direct drafts; a mode it does
    not support means it cannot run)."""
    fn = re.search(r"function effectiveMode\(role, act, chosen\) \{(.*?)\n\}", _script(html),
                   re.S)
    assert fn, "effectiveMode is missing"
    body = fn.group(1)
    assert "hasOwn(CEILING_MODES, role)" in body and "hasOwn(row, act.side_effect)" in body
    assert 'act.side_effect === "read" ? "direct" : chosen' in body
    assert 'if (!modes.includes("direct")) mode = "draft";' in body
    assert 'return modes.includes(mode) ? mode : "";' in body
    script = _script(html)
    # Every ticked action gets a badge, and the ceiling is named when it is the reason.
    assert "(capped by ceiling ${role})" in script
    assert "effectiveBadge(role, a, modeSel.value)" in script


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's JS")
def test_effective_mode_js_agrees_with_the_broker(html, tmp_path):
    """Where node exists, run the page's own effectiveMode (with its table)
    for every action of every vendored manifest, every ceiling (plus an
    unknown one) and both modes, and require role_ceiling.mode_under's
    answer each time. The structural test above holds when node does not."""
    from broker import role_ceiling
    script = _script(html)
    parts = [re.search(pat, script, re.S).group(0) for pat in (
        r"const hasOwn = [^\n]*\n", r"const ROLES = \[.*?\];",
        r"const CEILING_MODES = \{.*?\n\};",
        r"function effectiveMode\(role, act, chosen\) \{.*?\n\}")]
    targets = Path(__file__).resolve().parents[1] / "broker" / "targets"
    manifests = [load_manifest(ECHO_DIR / "manifest.yaml"),
                 *(load_manifest(p) for p in sorted(targets.glob("*/manifest.yaml")))]
    cases, expected = [], []
    for m in manifests:
        for a in m.actions:
            for role in (*roles.ROLES, "no-such-role"):
                for mode in ("direct", "draft"):
                    cases.append({"role": role, "mode": mode, "act": {
                        "name": a.name, "side_effect": a.side_effect,
                        "modes": list(a.effective_modes)}})
                    expected.append(role_ceiling.mode_under(m, a.name, mode, role) or "")
    js = tmp_path / "effective.js"
    js.write_text("\n".join(parts) + f"\nconst cases = {json.dumps(cases)};\n"
                  "console.log(JSON.stringify(cases.map((c) => "
                  "effectiveMode(c.role, c.act, c.mode))));\n", encoding="utf-8")
    out = subprocess.run(["node", str(js)], capture_output=True, text=True, check=True,
                         timeout=60).stdout
    assert json.loads(out) == expected


def test_ceiling_field_label_help_and_default(html):
    script = _script(html)
    # Both key dialogs call the field "Ceiling (role)"; the old "Role" label is gone.
    assert script.count('h("label", null, "Ceiling (role)", role.sel)') == 2
    assert 'h("label", null, "Role"' not in script
    help_line = re.search(r'const CEILING_LINE = "([^"]+)";', script).group(1)
    assert help_line == ("Never grants; caps every capability below it. "
                         "full = capabilities decide.")
    # A new key's ceiling defaults to full, the broker's owner default.
    default = re.search(r'const DEFAULT_CEILING = "([^"]+)";', script).group(1)
    assert default == auth.OWNER_KEY_DEFAULT_ROLE == "full"
    assert "roleSelect(DEFAULT_CEILING, " in script
    assert 'roleSelect("read-only")' not in script
    # An unknown stored role shows as the lowest ceiling, never a raise.
    assert "if (sel.value !== value) sel.value = ROLES[0];" in script


def test_keys_list_shows_the_ceiling_only_below_full(html):
    script = _script(html)
    assert '<th>Ceiling</th>' in html and "<th>Role</th>" not in html
    assert 'ceil !== "full" ? [badge(ceil, "warn")' in script
    assert 'n.role !== "full" ? badge("ceiling " + n.role, "warn") : null' in script


def test_grant_card_shows_the_brokers_ceiling_note(html):
    script = _script(html)
    note = ('if (g.ceiling_note) card.append('
            'h("div", { class: "warnbox ceilingnote" }, g.ceiling_note));')
    assert note in script


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


def test_enforcement_display_fails_closed_like_the_broker(html):
    """The badge shows what enforced_where would say: the plugin's live
    report when it is a known value, otherwise proxy (never the manifest's
    claim alone)."""
    from broker.plugins import settings as plugin_settings
    assert _js_list(html, "ENFORCEMENT_VALUES") == list(plugin_settings.ENFORCEMENT_VALUES)
    assert 'return ENFORCEMENT_VALUES.includes(live) ? live : "proxy";' in _script(html)


def test_views_nav_and_loaders_agree(html):
    sections = re.findall(r'<section class="view" data-view="([a-z]+)"', html)
    nav = re.findall(r'<a href="#/([a-z]+)" data-view="\1">', html)
    assert sorted(sections) == sorted(nav) == sorted(_js_list(html, "VIEWS"))
    loaders = re.search(r"const VIEW_REFRESH = \{(.*?)\n\};", _script(html), re.S).group(1)
    assert set(re.findall(r"^\s{2}([a-z]+):", loaders, re.M)) == set(sections)


def test_pass2_views_are_real_views(html):
    """Channels, Settings and Delegations were placeholders in pass 1."""
    assert "Available after the next merge." not in html
    assert 'class="card placeholder"' not in html
    for view, marker in (("channels", 'id="tgTokenForm"'), ("settings", 'id="settingsForm"'),
                         ("delegations", 'id="treeRoot"')):
        sec = re.search(rf'<section class="view" data-view="{view}".*?</section>', html, re.S).group(0)
        assert marker in sec, view


# ---- Telegram bot token: write-only ----------------------------------------------

def test_telegram_token_field_is_write_only_in_the_markup(html):
    tags = re.findall(r"<input\b[^>]*\bid=\"tgToken\"[^>]*>", _markup(html))
    assert len(tags) == 1
    tag = tags[0]
    assert 'type="password"' in tag
    assert 'autocomplete="new-password"' in tag
    assert not re.search(r"\svalue\s*=", tag)
    # It sits in a form whose native submit the CSP refuses (form-action 'none').
    assert re.search(r'<form class="secretrow" id="tgTokenForm"[^>]*>\s*<input\b[^>]*id="tgToken"', html)


def test_telegram_token_is_never_written_back(html):
    script = _script(html)
    # The only writes to the input empty it.
    writes = re.findall(r'\$\("tgToken"\)\.value\s*=(?!=)\s*([^;]+);', script)
    assert writes and set(writes) == {'""'}
    assert not re.search(r"tgToken[^;\n]*setAttribute", script)
    # The status's `token` field is a state word; code only ever compares it.
    uses = re.findall(r"\btg\.token\b(.{0,6})", _strip_comments(script))
    assert uses and all(re.match(r'\s*[!=]==\s*"', u) for u in uses), uses
    # The submit handler clears the field before it sends anything.
    handler = re.search(r'\$\("tgTokenForm"\)\.onsubmit = async \(e\) => \{(.*?)\n\};', script, re.S).group(1)
    assert handler.index('$("tgToken").value = "";') < handler.index("api(")


def test_settings_view_renders_every_setting_type(html):
    kinds = {spec.kind for spec in runtime_settings.SPECS.values()}
    assert kinds == {"int", "float", "hosts"}
    assert _object_keys(_block(html, "setting-inputs")) == kinds


def test_settings_view_flags_what_applies_only_at_start(html):
    # mcp_allowed_hosts_extra is read when the MCP session manager starts.
    assert re.search(r"mcp_allowed_hosts_extra: \"Applies at the next broker start", _script(html))


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
                 "/v1/admin/plugins/{x}/connect/qr.png",
                 "/v1/admin/telegram", "/v1/admin/telegram/token", "/v1/admin/telegram/link/start",
                 "/v1/admin/telegram/enable", "/v1/admin/telegram/disable",
                 "/v1/admin/telegram/test", "/v1/admin/telegram/unlink",
                 "/v1/admin/settings", "/v1/admin/keys/tree",
                 "/v1/admin/plugins/{x}/connect/finish", "/v1/admin/grants/{x}/revoke",
                 # + Add plugin, the job panel, installed provenance, the offered card
                 "/v1/admin/plugins/install/status", "/v1/admin/plugins/install/inspect",
                 "/v1/admin/plugins/install/git-token",
                 "/v1/admin/plugins/install", "/v1/admin/plugins/install/jobs/{x}",
                 "/v1/admin/plugins/installed", "/v1/admin/plugins/{x}/upgrade",
                 "/v1/admin/plugins/{x}/remove", "/v1/admin/plugins/offered",
                 "/v1/admin/plugins/{x}/pin"):
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


def test_shared_slot_markers_reach_the_console():
    """The Google plugins share one account card: the console groups plugins
    by connection.shared and edits the config fields marked shared once. Both
    markers are in the data it reads (the vendored manifests, through
    settings.schema_view and the admin view's connection block)."""
    from broker.plugins import settings as plugin_settings
    targets = Path(__file__).resolve().parents[1] / "broker" / "targets"
    google = {}
    for pid in ("gmail", "gcal", "gdrive"):
        m = load_manifest(targets / pid / "manifest.yaml")
        google[pid] = m
        assert m.connection.shared == "google"
        shared = {f["name"] for f in plugin_settings.schema_view(m) if f["shared"]}
        assert shared == {"client_id", "client_secret"}


def test_console_groups_plugins_by_shared_slot(html):
    script = _script(html)
    assert "function slotOf(p) { return (p && p.connection && p.connection.shared) || null; }" in script
    assert "schema.filter((f) => f.shared)" in script and "schema.filter((f) => !f.shared)" in script


# ---- external plugins: + Add plugin, the job panel, the offered card -----------------

def _section(html: str, view: str) -> str:
    return re.search(rf'<section class="view" data-view="{view}".*?</section>', html, re.S).group(0)


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to parse the page's JS")
def test_the_script_parses(html, tmp_path):
    js = tmp_path / "console.js"
    js.write_text(_script(html), encoding="utf-8")
    r = subprocess.run(["node", "--check", str(js)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr


def test_plugins_view_has_add_plugin_in_its_header_and_the_new_panels(html):
    sec = _section(html, "plugins")
    assert ('<div class="viewhead"><h2>Plugins</h2><button class="primary" type="button" '
            'id="addPluginBtn">+ Add plugin</button></div>') in sec
    for panel in ("pluginJob", "pluginList", "pluginInstalled", "pluginOffered"):
        assert f'<div id="{panel}"></div>' in sec, panel
    script = _script(html)
    assert '$("addPluginBtn").onclick = () => openAddPlugin({});' in script


def test_the_refused_card_became_offered_awaiting_review(html):
    script = _script(html)
    assert "Refused plugins" not in html and "pluginRefused" not in html
    assert 'h("h3", null, "Offered, awaiting review")' in script
    # A pin button only where a pin would fix it; the reason otherwise.
    assert 'if (it.pinnable) {' in script and '"Review and pin"' in script
    assert "const why = it.pinnable ? it.reason : (it.blocked || it.error || it.reason);" in script


def test_installed_plugins_show_where_they_came_from_with_upgrade_and_remove(html):
    script = _script(html)
    assert 'h("div", { class: "installedfrom" }, "installed from ",' in script
    assert 'h("span", { class: "mono" }, `${r.source}@${r.ref}`)' in script
    assert ('up.onclick = () => openAddPlugin({ service: r.service, source: r.source, '
            'ref: r.ref });') in script
    assert "rm.onclick = () => openRemovePlugin(r);" in script
    # Remove asks, and keeps the data unless the owner ticks purge.
    assert "body: { purge: purge.checked }" in script


def test_installer_off_explains_the_env_lines_and_the_command(html):
    """When the installer is off, the dialog shows the .env lines and the
    command instead of Inspect; its fallbacks match the broker's."""
    from broker.services import plugin_install
    script = _script(html)
    assert "if (!st || !st.configured) { openInstallerOff(st); return; }" in script
    assert f'"{plugin_install.MANUAL_COMMAND}"' in script
    assert '"INSTALLER_ENABLED=true"' in script and plugin_install.ENABLE_LINES[0] == (
        "INSTALLER_ENABLED=true")
    assert "docker login ghcr.io" in script


def test_install_goes_through_inspect_and_sends_only_the_reviewed_commit(html):
    script = _script(html)
    assert 'api("/v1/admin/plugins/install/inspect", { method: "POST", body: { source, ref } })' \
        in script
    assert "const body = { source: r.source, ref: r.ref, commit: r.commit };" in script
    # Problems (invalid or blocked manifests) leave no Install button.
    assert "m.setButtons(problems.length ? [back] : [back, go]);" in script


def test_the_job_panel_polls_every_two_seconds_through_the_brokers_restart(html):
    script = _script(html)
    assert "const JOB_POLL_MS = 2000;" in script
    # No connection, or the edge saying nothing answers: keep polling.
    assert "const RESTART_STATUSES = [0, 502, 503, 504];" in script
    assert "if (!RESTART_STATUSES.includes(e.status)) {" in script
    assert "run.restarting = true;" in script and "The broker is being recreated" in script
    assert 'if (st === "done" || st === "failed") run.stopped = true;' in script
    assert "const log = listOf(j.log);" in script and 'h("pre", { class: "joblog" }' in script
    assert script.count("setTimeout(() => pollJob(run), JOB_POLL_MS)") == 3
    # Signing out stops it.
    show_gate = re.search(r"function showGate\(which, msg\) \{(.*?)\n\}", script, re.S).group(1)
    assert "stopJob();" in show_gate


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's JS")
def test_the_review_card_renders_the_brokers_review(html, tmp_path):
    """Run the page's own review functions under node, with a minimal DOM,
    on what the broker really returns (pins.summary / pins.diff of the echo
    manifest and an upgrade of it): no exception, and the facts an owner
    approves are all on the card."""
    import yaml

    from broker.plugins import pins
    from broker.plugins.manifest import load_manifest_text

    old = load_manifest(ECHO_DIR / "manifest.yaml")
    data = yaml.safe_load((ECHO_DIR / "manifest.yaml").read_text(encoding="utf-8"))
    data["version"] = "0.2.0"
    data["actions"].append({"name": "purge_room", "side_effect": "destructive"})
    data["config_schema"].append({"name": "webhook_key", "type": "string", "secret": True})
    new = load_manifest_text(yaml.safe_dump(data, sort_keys=False))
    desc = {"service": "echo", "plugins": ["echo"], "runtime": "0.3",
            "build": {"dockerfile": "Dockerfile"}, "volumes": {"echo_data": "/data"},
            "environment": {"ECHO_DB": "/data/echo.db"}, "env_passthrough": ["TZ"]}
    review = {"source": "github.com/acme/aab-plugin-echo", "ref": "v0.2.0", "commit": "b" * 40,
              "service": "echo", "descriptor": desc, "upgrade": True,
              "installed": {"ref": "v0.1.0", "commit": "a" * 40},
              "plugins": [{"id": "echo", "version": "0.2.0", "valid": True, "error": None,
                           "summary": pins.summary(new), "diff": pins.diff(old, new),
                           "pinned": {"version": "0.1.0"}, "blocked": None},
                          {"id": "evil", "version": "1.0.0", "valid": False,
                           "error": "actions: too short", "summary": None, "diff": None,
                           "pinned": None, "blocked": None}],
              "problems": ["evil: actions: too short"]}
    script = _script(html)
    names = ("showControls", "isSafeUrl", "badge", "fact", "idChips", "listOf", "shortCommit",
             "objectOr", "objectsOf", "chipsOr", "reviewPlugin", "changeList", "diffView",
             "packageReview", "inspectReview", "h")
    parts = [re.search(r"const BIDI_CONTROLS = [^\n]*\n", script).group(0),
             re.search(r"const hasOwn = [^\n]*\n", script).group(0)]
    parts += [re.search(rf"^function {n}\(.*?^\}}\n", script, re.S | re.M).group(0)
              for n in names]
    dom = """
class Node { constructor() { this.children = []; } append(...k) { this.children.push(...k); }
  get childElementCount() { return this.children.filter((c) => c instanceof El).length; } }
class Text extends Node { constructor(t) { super(); this.text = t; } }
class El extends Node { constructor(tag) { super(); this.tag = tag; this.attrs = {}; }
  setAttribute(k, v) { this.attrs[k] = v; } set textContent(t) { this.children = [new Text(t)]; } }
const document = { createElement: (t) => new El(t), createTextNode: (t) => new Text(t) };
const location = { href: "http://console.invalid/", origin: "http://console.invalid" };
const text = (n) => (n instanceof Text ? n.text : n.children.map(text).join(" "));
"""
    js = tmp_path / "review.js"
    js.write_text(dom + "\n".join(parts) + f"\nconst r = {json.dumps(review)};\n"
                  "console.log(text(inspectReview(r, r.problems)));\n", encoding="utf-8")
    out = subprocess.run(["node", str(js)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    card = " ".join(out.stdout.split())
    for want in ("github.com/acme/aab-plugin-echo@v0.2.0", "b" * 40, "plugin-echo", "net_echo",
                 "an upgrade of echo from v0.1.0", "Cannot go ahead: evil: actions: too short",
                 "post_item write modes: direct, draft", "delete_item destructive",
                 "api_secret secret", "What changes from the pinned v0.1.0 to v0.2.0",
                 "added purge_room", "New secrets webhook_key", "echo_secrets at /secrets",
                 "echo_data at /data", "ECHO_DB=/data/echo.db", "From the host's .env TZ",
                 "invalid actions: too short"):
        assert want in card, want


# ---- the GitHub token for private repositories: write-only --------------------------

def _function(script: str, name: str) -> str:
    return re.search(rf"^(?:async )?function {name}\(.*?^\}}\n", script, re.S | re.M).group(0)


def test_the_git_token_field_is_write_only(html):
    script = _script(html)
    fn = _function(script, "gitTokenSection")
    # A password field with no autofill, in its own small form inside the dialog.
    assert re.search(r'const input = h\("input", \{ type: "password", '
                     r'autocomplete: "new-password",', fn)
    assert 'h("form", { class: "secretrow", autocomplete: "off" }, input, set, clear)' in fn
    # The only writes to the field empty it, and the submit handler does so
    # before it sends anything.
    writes = re.findall(r"\binput\.value\s*=(?!=)\s*([^;]+);", fn)
    assert writes == ['""']
    assert not re.search(r"input[^;\n]*setAttribute", fn)
    handler = re.search(r"form\.onsubmit = async \(e\) => \{(.*?)\n  \};", fn, re.S).group(1)
    assert handler.index('input.value = "";') < handler.index("api(")
    assert ('api("/v1/admin/plugins/install/git-token", { method: "POST", '
            'body: { token: value } })') in handler
    assert 'api("/v1/admin/plugins/install/git-token", { method: "DELETE" })' in fn
    # The broker answers with a state word; code only ever compares it.
    uses = re.findall(r"\.git_token\b(.{0,6})", _strip_comments(script))
    assert uses and all(re.match(r'\s*[!=]==\s*"', u) for u in uses), uses
    # The help line says what the owner is trusting it with.
    for words in ("read-only", "stores it encrypted", "never shows it again",
                  "installer's allowlist"):
        assert words in fn, words


def test_the_add_plugin_dialog_carries_the_git_token_section(html):
    script = _script(html)
    opener = _function(script, "openAddPlugin")
    assert "const body = h(\"div\", null, form, gitTokenSection(st));" in opener
    assert "openModal({ title, body, wide: true, sticky: true, buttons: formButtons });" in opener
    # Back from the review restores the form and the token section together.
    assert "mm.setBody(f.body);" in _function(script, "inspectPackage")
    # The installer-off dialog says where the token goes instead of .env.
    assert "It is not a line in .env" in _function(script, "openInstallerOff")
    assert "INSTALLER_GIT_TOKEN" not in html


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's JS")
def test_the_git_token_section_shows_each_state_and_the_brokers_shape_rule(html, tmp_path):
    """Run the page's own section under node with a minimal DOM for each
    state the broker reports, and its shape check against the broker's."""
    from broker.services.install_git_token import TOKEN_RE

    script = _script(html)
    parts = [re.search(r"const BIDI_CONTROLS = [^\n]*\n", script).group(0),
             re.search(r"const GIT_TOKEN_SHAPE = [^\n]*\n", script).group(0)]
    parts += [_function(script, n) for n in ("showControls", "isSafeUrl", "badge", "h",
                                              "gitTokenSection")]
    states = [{"git_token": "unset", "secrets_key_configured": True},
              {"git_token": "set", "secrets_key_configured": True},
              {"git_token": "unreadable", "secrets_key_configured": True},
              {"git_token": "unset", "secrets_key_configured": False}]
    samples = ["x" * 20, "x" * 19, "x" * 255, "x" * 256, "has space 0123456789abcdef",
               "tab\t0123456789abcdefghij", "é" + "x" * 20, "x" * 20 + "\n",
               "$(a)`b`'\";|&<>*?%s\\{}[]!#~0123"]
    dom = """
class Node { constructor() { this.children = []; } append(...k) { this.children.push(...k); }
  replaceChildren(...k) { this.children = k.map((c) => (typeof c === "string" ? new Text(c) : c)); } }
class Text extends Node { constructor(t) { super(); this.text = t; } }
class El extends Node { constructor(tag) { super(); this.tag = tag; this.attrs = {}; }
  setAttribute(k, v) { this.attrs[k] = v; } set textContent(t) { this.children = [new Text(t)]; } }
const document = { createElement: (t) => new El(t), createTextNode: (t) => new Text(t) };
const location = { href: "http://console.invalid/", origin: "http://console.invalid" };
const text = (n) => (n instanceof Text ? n.text : n.children.map(text).join(" "));
const all = (n, pred, out = []) => { if (n instanceof El && pred(n)) out.push(n);
  for (const c of n.children) all(c, pred, out); return out; };
"""
    run = f"""
const out = [];
for (const st of {json.dumps(states)}) {{
  const sec = gitTokenSection(st);
  const [set, clear] = all(sec, (n) => n.tag === "button");
  const [warn] = all(sec, (n) => n.className === "warnbox");
  const [input] = all(sec, (n) => n.tag === "input");
  out.push({{ text: text(sec), set: text(set), setDisabled: Boolean(set.disabled),
             clearHidden: Boolean(clear.hidden), warnHidden: Boolean(warn.hidden),
             type: input.attrs.type, value: input.value === undefined ? null : input.value }});
}}
out.push({json.dumps(samples)}.map((s) => GIT_TOKEN_SHAPE.test(s)));
console.log(JSON.stringify(out));
"""
    js = tmp_path / "gittoken.js"
    js.write_text(dom + "\n".join(parts) + run, encoding="utf-8")
    proc = subprocess.run(["node", str(js)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    *views, shapes = json.loads(proc.stdout)
    unset, stored, unreadable, nokey = views
    assert "not set" in unset["text"] and unset["set"] == "Set" and unset["clearHidden"]
    assert stored["text"].startswith("GitHub token for private plugin repositories set")
    assert stored["set"] == "Replace" and not stored["clearHidden"]
    assert "re-enter required" in unreadable["text"] and "BROKER_SECRETS_KEY changed" in (
        unreadable["text"])
    assert unreadable["set"] == "Replace" and not unreadable["clearHidden"]
    for v in (unset, stored, unreadable):
        assert v["warnHidden"] and not v["setDisabled"]
    assert nokey["setDisabled"] and not nokey["warnHidden"]
    assert all(v["type"] == "password" and v["value"] is None for v in views)
    # The page refuses exactly what the broker refuses.
    assert shapes == [bool(TOKEN_RE.fullmatch(s)) for s in samples]
