# Owner console

The console is how the owner runs the broker: first-time setup, sign-in,
plugins, agent keys and their capabilities, approvals, hidden resources, the
decision record, and the owner's own account. It is one static file,
`broker/broker/templates/console.html`: vanilla JavaScript, a hash router over
`section[data-view]`, no build step, nothing loaded from outside the broker.
`broker/broker/routers/console.py` serves it at `/admin` and at every
`/admin/...` path (so `/admin/keys` works as a deep link).

## Sign-in flow

1. On load the page calls `GET /auth/status`.
   - `setup_completed: false`: the setup page (`POST /auth/setup` with the
     one-time `SETUP_TOKEN`, a username and a password).
   - `login_required: true`: the login page (`POST /auth/login`), which sets
     the `aab_session` cookie (HttpOnly, SameSite=Strict).
   - otherwise the app, after `GET /auth/me` names the owner.
2. Every request sends `X-Requested-With: aab-console` (the value of
   `deps.CSRF_VALUE`; a test keeps them equal). With the SameSite=Strict cookie
   this is the CSRF defence; see `docs/auth.md`.
3. A 401 from any call means the session is gone: the page drops back to the
   login screen, closes any dialog, and stops polling.
4. Log out is `POST /auth/logout`.

The page itself holds no data, so fetching it needs no owner credential. When
Cloudflare Access is enabled the page requires the Access identity, exactly
like `/auth/*`.

## Views

| View (`data-view`) | What it does | API |
|---|---|---|
| `overview` | Counts of pending actions, permission requests, scheduled actions and active keys; a status card per plugin; the decision-chain verification. | `/v1/admin/actions`, `/v1/admin/grants`, `/v1/admin/plugins`, `/v1/admin/keys`, `/v1/admin/decisions/verify` |
| `requests` | Actions awaiting approval (summary rendered from the manifest's `summary_template`, key chain when delegated, resource label and id, agent note, run time, all params) and permission requests (each capability spelled out: actions by side effect, selectors, constraints, mode, expiry, and the budget it **adds**). Approve / reject. | `/v1/admin/actions?status=pending`, `/v1/admin/actions/{id}/approve\|reject`, `/v1/admin/grants?status=pending`, `/v1/admin/grants/{id}/approve\|reject` |
| `scheduled` | Actions waiting for their `run_at`, with cancel. | `/v1/admin/actions?status=scheduled`, `/v1/admin/actions/{id}/cancel` |
| `decisions` | The decision record filtered by key, plugin, decision and time; key chain root to leaf, grant chain, `enforced_where` per dimension, outcome rows; "Verify chain". | `/v1/admin/decisions`, `/v1/admin/decisions/verify` |
| `plugins` | Per plugin: enable / disable, the config form generated from `config_schema`, health, and a connection panel by `connection.kind`. | `/v1/admin/plugins[/{id}]`, `.../enable`, `.../disable`, `.../health`, `.../connect/start`, `.../connect/finish`, `.../connect/qr.png`, `.../disconnect` |
| `keys` | Key list; create (with the capability editor and denies, plaintext shown once); edit role, rate, expiry, disabled, denies and the root grant's capabilities; revoke other grants; rotate (new plaintext once); disable. | `/v1/admin/keys[/{id}]`, `/v1/admin/keys/{id}/rotate`, `/v1/admin/grants/{id}/revoke` |
| `hidden` | Hide a resource by picking it by name (the label is captured at hide time); list; unhide. | `/v1/admin/hidden`, `/v1/admin/resolve` |
| `account` | Change password; admin tokens (create with plaintext once, list, revoke); sessions (current one marked, revoke). | `/v1/admin/password`, `/v1/admin/tokens`, `/v1/admin/sessions` |
| `channels`, `settings`, `delegations` | Placeholders: "Available after the next merge." Pass 2 fills them in place. | |

The badges and the overview, requests and scheduled views refresh every 15 s.
Views with forms never auto-refresh, so a poll can never discard typing; the
refresh button reloads the current view.

## Generated from the manifests

`GET /v1/admin/plugins` carries, per plugin, a `manifest` projection built by
`broker/broker/plugins/manifest_view.py`:

- `actions`: name, `side_effect`, effective `modes`, `resource`,
  `selector_param`, `schedulable`, `summary_template`, `doc`;
- `resources`: per kind, `display`, `resolve`, `hideable`, `id_format`;
- `selectors`: the set-form narrowings (list, subtree, pattern), stored in a
  capability's `selector`;
- `constraints`: every scalar form (range, flag, level), whether the manifest
  declared it as a narrowing or a constraint, stored in `constraints`.

Derived narrowings (GitHub `permissions`, Google scopes) are left out: no grant
can set them. The split matches `authority/capability.target_forms`, and a test
holds the two together.

The capability editor turns that into, per plugin: action checkboxes grouped by
side effect with `all` / `all reads` / `all writes` / `all destructive`
shortcuts (sent as explicit names, never globs); a selector per set-form
dimension, "any" by default, with a resource picker for "only these"; one input
per scalar constraint (range: a number, empty = no limit; flag: "allowed",
checked = unrestricted and therefore omitted; level: the ordered values, the
highest = unrestricted and omitted); mode (draft or direct, shown only when a
write is selected, since reads are always direct); a per-minute and per-day
budget (writes only; reads are never charged); and an expiry. New capabilities
start from the manifest's constraint defaults. The broker normalizes whatever
is sent (it may split reads and writes into two capabilities), so the edit
view can show a grant as more blocks than were entered.

`resourcePicker(target, kind)` searches `/v1/admin/resolve` by name when the
resource kind can resolve, and always offers the typed text as an id. What is
stored is the id; the label is display only, and the id stays visible next to
it (labels are other people's text).

### What the console reads from a plugin's status

The connection panel reads the plugin's `/status` answer as stored in
`last_health` (a nested `connection` object, when present, is merged over the
top level). Plugin lanes should report these where they apply:

| Field | Used by |
|---|---|
| `connected`, `healthy` | every badge |
| `waiting_for_qr` | `sidecar_qr`: while true, the QR image is re-fetched every 5 s; once `connected`, the console calls `connect/finish` once |
| `push_name`, `account`, `login`, `email`, `jid` | the "connected as" line (first one present) |
| `permissions` (object) | `github_app`: the installed permission set |
| `granted_scopes`, `missing_scopes` (lists) | `google_oauth` |
| `<secret field>_set` (boolean) | secret config fields show "set" / "not set" and "Set" / "Replace"; without it they say only "stored in the plugin" |

`connect/start` must answer an `https:` `url` for `github_app` and
`google_oauth`; anything else is refused with a message, never opened.

## Security design

- **Nonce CSP.** Every response carries a fresh nonce:
  `default-src 'self'; script-src 'nonce-...'; style-src 'self' 'unsafe-inline';
  img-src 'self' data:; connect-src 'self'; frame-ancestors 'none';
  base-uri 'none'; form-action 'none'; object-src 'none'`, plus
  `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`,
  `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`. The console shows
  text that agents control (notes, params, labels, delegated key names) to the
  person who approves those agents. If an escaping slip ever let markup
  through, the nonce policy still refuses to run it, so the slip cannot become
  "the agent approves its own request". `form-action 'none'` means a form whose
  script failed can never fall back to a native submit that puts a password
  in a URL.
- **No HTML-string sinks.** All DOM is built by `h()` (attributes through
  `setAttribute`, text through text nodes) and `textContent`. The file contains
  no `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `document.write`, `eval`
  or inline event-handler attributes; tests enforce all of it.
- **Agent text is marked as such.** It sits in a quoted, bidi-isolated box.
  Unicode embedding, override and isolate controls (U+202A-202E,
  U+2066-2069) are rendered as visible `[U+202E]` markers, so an override
  cannot reorder a summary or a file name on an approval card. LRM/RLM are left
  alone (Hebrew and Arabic names use them).
- **One-time secrets.** A new agent key, a rotated key or a new admin token is
  shown once in a copy box and nowhere else; the admin-token banner is cleared
  on navigation. Secret config fields are write-only: the value is sent once to
  the plugin (through the broker, which keeps none of it) and cleared from the
  input immediately, whether or not the call succeeds.
- **Links from plugins** (GitHub install, Google consent) must be `https:`;
  they open with `rel="noopener noreferrer"`.
- **Confirmation** precedes every destructive or disruptive step (disable a
  plugin, disconnect, disable or rotate a key, revoke a grant, token or
  session, unhide, cancel a scheduled action, clear a secret). Dialogs focus
  their safe button, and dialogs holding a form close only through their
  buttons.

## Changing the console

- Add a view as a `section class="view" data-view="..."` plus a nav link and an
  entry in `VIEWS`; put its loader in `VIEW_REFRESH`, and in `LIVE_VIEWS` only
  if it has no form. Pass 2 fills `channels`, `settings` and `delegations`.
- Build DOM with `h()`; assign handlers in script (`el.onclick = ...`).
- Write API paths as literals that begin with the path (`"/v1/admin/..."` or a
  template literal `` `/v1/admin/keys/${enc(id)}` ``): the test extracts every
  `/v1/...` and `/auth/...` literal and checks it against the app's OpenAPI
  path table. Do not mention a path in a comment unless it exists.
- Send everything through `api()` (CSRF header, 401 handling, error text).

`broker/tests/test_console.py` covers: headers and nonce, identical page for
every `/admin` path, no owner credential needed (and no data in the page),
Cloudflare Access applies, no external URLs, no HTML sinks, no inline
handlers, no hidden bidi controls in the source, CSRF constant, role list,
form vocabulary, a config renderer per manifest field type, a connection
panel per connection kind, views and placeholders, every API path exists, and
the manifest projection.
