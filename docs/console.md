# Owner console

The owner runs the broker from the console. The console covers these areas:

- First-time setup and sign-in.
- Plugins.
- Agent keys and their capabilities.
- The delegation tree.
- Approvals.
- Hidden resources.
- The Telegram approval channel.
- Operator settings.
- The decision record.
- The owner's own account.

The console is one static file, `broker/broker/templates/console.html`. It uses
vanilla JavaScript and a hash router over `section[data-view]`. It has no build
step and loads nothing from outside the broker.
`broker/broker/routers/console.py` serves it at `/admin` and at every
`/admin/...` path, so `/admin/keys` works as a deep link.

## Sign-in flow

1. On load, the page calls `GET /auth/status`.
   - `setup_completed: false`: the page shows the setup page (`POST /auth/setup`
     with the one-time `SETUP_TOKEN`, a username and a password).
   - `login_required: true`: the page shows the login page (`POST /auth/login`).
     Login sets the `aab_session` cookie (HttpOnly, SameSite=Strict).
   - Otherwise, the page shows the app, after `GET /auth/me` names the owner.
2. Every request sends `X-Requested-With: aab-console`. That is the value of
   `deps.CSRF_VALUE`, and a test keeps them equal. With the SameSite=Strict
   cookie, this is the CSRF defence (see `docs/auth.md`).
3. A 401 from any call means that the session is gone. The page goes back to
   the login screen, closes any dialog, and stops polling.
4. Log out is `POST /auth/logout`.

The page itself holds no data, so a fetch of the page needs no owner
credential. When Cloudflare Access is on, the page requires the Access
identity, exactly like `/auth/*`.

## Views

| View (`data-view`) | What it does | API |
|---|---|---|
| `overview` | Counts of pending actions, permission requests, scheduled actions and active keys; a status card per plugin; the decision-chain verification. | `/v1/admin/actions`, `/v1/admin/grants`, `/v1/admin/plugins`, `/v1/admin/keys`, `/v1/admin/decisions/verify` |
| `requests` | Actions awaiting approval (summary made from the manifest's `summary_template`, key chain when delegated, resource label and id, agent note, run time, all params) and permission requests (each capability spelled out: actions by side effect, selectors, constraints, mode, expiry, and the budget it **adds**), with a warning when the key's ceiling would cap the request (see "The ceiling (role)"). Approve / reject. | `/v1/admin/actions?status=pending`, `/v1/admin/actions/{id}/approve\|reject`, `/v1/admin/grants?status=pending`, `/v1/admin/grants/{id}/approve\|reject` |
| `scheduled` | Actions waiting for their `run_at`, with cancel. | `/v1/admin/actions?status=scheduled`, `/v1/admin/actions/{id}/cancel` |
| `decisions` | The decision record filtered by key, plugin, decision and time; key chain root to leaf, grant chain, `enforced_where` per dimension, outcome rows; "Verify chain". | `/v1/admin/decisions`, `/v1/admin/decisions/verify` |
| `plugins` | Per plugin: enable / disable, the config form generated from `config_schema`, health, and a connection panel by `connection.kind`. Plugins that share a connection slot get one shared card above their own (see below). **+ Add plugin** installs an external plugin from its repository; installed plugins show where they came from with Upgrade and Remove; offered manifests wait for review; the Add plugin dialog holds the write-only GitHub token for private repositories (see "Adding a plugin from its repository"). | `/v1/admin/plugins[/{id}]`, `.../enable`, `.../disable`, `.../health`, `.../connect/start`, `.../connect/finish`, `.../connect/qr.png`, `.../disconnect`; `/v1/admin/plugins/install/status`, `.../install/git-token`, `.../install/inspect`, `.../install`, `.../install/jobs/{id}`, `/v1/admin/plugins/installed`, `/v1/admin/plugins/{service}/upgrade`, `.../{service}/remove`, `/v1/admin/plugins/offered`, `/v1/admin/plugins/{id}/pin` |
| `keys` | Key list (the ceiling shown only below `full`); create (with the capability editor, the ceiling defaulting to `full`, and denies, plaintext shown once); edit the ceiling (role), rate, expiry, disabled, denies and the root grant's capabilities; revoke other grants; rotate (new plaintext once); disable; a "Tree" link for keys in a delegation chain. | `/v1/admin/keys[/{id}]`, `/v1/admin/keys/{id}/rotate`, `/v1/admin/grants/{id}/revoke` |
| `delegations` | The key forest: each key under the key it came from, with status, live, depth and orphan badges, why a key is not live, a grants summary and its capabilities; expand / collapse; edit, disable / enable, revoke. `#/delegations/<key id>` opens the tree on that key. | `/v1/admin/keys/tree`, `/v1/admin/keys/{id}` (PATCH), `/v1/admin/grants/{id}/revoke` |
| `hidden` | Hide a resource by picking it by name (the label is captured at hide time); list; unhide. | `/v1/admin/hidden`, `/v1/admin/resolve` |
| `channels` | Telegram: token state, bot name, set / replace / clear the bot token (write-only), link your chat (one-time code, waits until linked), enable / disable, test message, unlink, poll-loop health. | `/v1/admin/telegram`, `.../token` (POST, DELETE), `.../link/start`, `.../enable`, `.../disable`, `.../test`, `.../unlink` |
| `settings` | Every console-editable operator setting with its value, default, bounds and source; one form, only changed settings sent; reset to default. "What lives in files" lists the env-only keys and why. | `/v1/admin/settings` (GET, PATCH) |
| `account` | Change password; admin and monitor tokens (scope chosen at creation, plaintext once, list with scope, revoke); sessions (current one marked, revoke). | `/v1/admin/password`, `/v1/admin/tokens`, `/v1/admin/sessions` |

The header shows two pills: plugins up of plugins enabled, and whether
Telegram is on.

The badges and the overview, requests and scheduled views refresh every 15 s.
Views with forms never refresh automatically, so a poll can never discard
typing. The refresh button reloads the current view.

## Generated from the manifests

`GET /v1/admin/plugins` carries a `manifest` projection per plugin.
`broker/broker/plugins/manifest_view.py` builds it:

- `actions`: name, `side_effect`, effective `modes`, `resource`,
  `selector_param`, `schedulable`, `summary_template`, `doc`.
- `resources`: per kind, `display`, `resolve`, `hideable`, `id_format`.
- `selectors`: the set-form narrowings (list, subtree, pattern). A capability
  stores them in its `selector`.
- `constraints`: every scalar form (range, flag, level), whether the manifest
  declares it as a narrowing or as a constraint. A capability stores these in
  `constraints`.

The projection leaves out narrowings with `derived_from` (GitHub
`permissions`, Google scopes), because no grant can set them. The split
matches `authority/capability.target_forms`, and a test holds the two
together.

The capability editor turns that projection into these controls, per plugin:

- Action checkboxes, grouped by side effect, with `all` / `all reads` /
  `all writes` / `all destructive` shortcuts. The shortcuts send explicit
  names, never globs.
- A selector per set-form dimension. It is "any" by default, with a resource
  picker for "only these".
- One input per scalar constraint:
  - range: a number. Empty means no limit.
  - flag: the `allowed` checkbox. Checked means unrestricted, so the editor
    omits it.
  - level: the ordered values. The highest means unrestricted, so the editor
    omits it.
- A mode: draft or direct. The editor shows it only when the owner selects a
  write, because reads are always direct.
- A per-minute and per-day budget, for writes only. The broker never charges
  reads.
- An expiry.

New capabilities start from the manifest's constraint defaults. The broker
normalizes whatever the editor sends, and it can split reads and writes into
two capabilities. So the edit view can show a grant as more blocks than the
owner entered.

### The ceiling (role)

A key's role is its **ceiling**. It never grants anything: the capabilities
are the only grant. It caps every capability below it, per side effect:

- `read-only`: the broker denies writes and destructive actions.
- `read-draft`: writes and destructive actions run as drafts, even where a
  capability says direct.
- `read-act`: destructive actions run as drafts.
- `full`: caps nothing. The capabilities decide.

The console shows the ceiling in this way:

- **Create and edit dialogs.** The field label is "Ceiling (role)". The help
  line is "Never grants; caps every capability below it. full =
  capabilities decide." A further line describes the chosen ceiling. A new
  key's ceiling defaults to `full` (`DEFAULT_CEILING`). A test keeps that
  value equal to the broker's `auth.OWNER_KEY_DEFAULT_ROLE`. The owner can
  still select the lower ceilings. When the page does not know a stored role,
  it shows the lowest ceiling. Thus a save can never raise it unasked.
- **Effective mode per ticked action.** Next to every ticked action, the
  capability editor shows the real run mode of a call. It uses the ceiling
  chosen in the same dialog. It also says when the ceiling is the reason:
  - `draft (capped by ceiling read-draft)`.
  - `denied (capped by ceiling read-only)`.
  - `cannot run: it cannot be drafted, choose direct`, for a write the
    manifest cannot draft under a draft capability.

  The line repaints when the actions, the mode or the ceiling change.
  `effectiveMode()` calculates it with the broker's rule. That rule is
  `role_ceiling.mode_under`: the lower of the capability's mode and the
  ceiling's mode, then `policy.run_mode`. The input is `CEILING_MODES`, the
  effective-mode table. A test keeps that table equal to `roles.role_caps`.
- **Keys list and delegation tree.** These show the ceiling only when it is
  lower than `full`. The keys list shows the lowest role along the key's
  chain. That is the role that really caps the key, because the broker also
  meets the role of every ancestor. When that role is not the key's own, the
  list marks it "from a key above".
- **Requests view.** `GET /v1/admin/grants` carries two fields per grant:
  - `ceiling`: the lowest role along the chain of the requesting key.
  - `ceiling_note`: one sentence when that ceiling would cap what the request
    asks for, else null. Take a `read-draft` key that asks for a direct
    write. Its note says: "This key's ceiling is read-draft: post_item will
    still queue for your approval, whatever this grant says."

  The permission-request card shows a `ceiling` badge below `full`. It shows the
  note next to the Approve button. The reason is that approval of such a
  request does not do what the agent asked. The Telegram card carries the same
  note.

`resourcePicker(target, kind)` searches `/v1/admin/resolve` by name when the
resource kind can resolve. It always offers the typed text as an id. The
broker stores the id. The label is for display only, and the id stays visible
next to it, because labels are other people's text.

### Shared connection slots (the Google account card)

A manifest with `connection.shared` marks a connection that several plugins
use together. Gmail, Calendar and Drive all say `shared: google`. The config
fields with `shared: true` belong to that connection. The Plugins view draws
one card per slot, above the cards of its members. The card title comes from
the slot ("Google account").

- **Configuration:** only the shared fields (`client_id`, `client_secret`). A
  save or a secret goes through one member: an enabled one, when there is one.
  The broker copies shared non-secret values to every member, and it sends the
  secret once to the service. Then the console refreshes the health of every
  member.
- **Connection:** one Connect / Reconnect / Disconnect for the service. It
  shows the granted scopes. After the service connects, it also shows the
  scopes that an enabled member still needs as **reconnect needed**. The card
  offers Connect only when the owner has enabled at least one member. The
  reason is that the consent asks for exactly the scopes of the enabled
  members.
- **Member cards** keep enable / disable, their own non-shared fields (none
  for Google), their own health line, and their own granted and missing
  scopes. They point to the shared card for connecting.

### Connection flows

- `sidecar_qr` (WhatsApp): "Pair a device" calls `connect/start`. Then the
  console fetches the QR image again every 5 s while the plugin reports
  `waiting_for_qr`. When the plugin reports `connected`, the console calls
  `connect/finish` once.
- `github_app`: "Install the GitHub App" calls `connect/start`. The console
  shows the returned install URL (https only) as a link and as text. GitHub's
  redirect to the broker's callback page finishes the install. Otherwise, the
  installation id field posts `installation_id` **plus the `state`
  connect/start issued** to `connect/finish`. The plugin rejects a missing or
  reused state. After the install, the panel shows the account, the installed
  permission set and the repository selection. In PAT mode (`mode: pat`) there
  is nothing to install: `connect/start` answers `kind: none`. Then the panel
  says that every restriction is proxy-enforced, and offers only Disconnect.
- `google_oauth`: "Connect Google" opens the consent URL from `connect/start`
  (https only). The console also shows a link, in case a popup blocker stops
  the new tab. Google redirects to the broker's `/oauth/callback/google` page,
  which finishes the connect. When the owner comes back to the console tab,
  the plugin states reload.

### What the console reads from a plugin's status

The connection panel reads the plugin's `/status` answer, as stored in
`last_health`. A nested `connection` object, when present, goes over the top
level. Make a plugin report these fields where they apply:

| Field | Used by |
|---|---|
| `connected`, `healthy` | every badge |
| `health` (string) | the status line under a plugin's name when it is not `ok` (for example `reconnect needed: scopes missing`) |
| `enforcement` | the enforcement badge and note: the live value when it is `target`, `mixed` or `proxy`; otherwise **proxy (not reported)**, exactly as `policy.enforced_where` counts it, never the manifest's claim alone |
| `waiting_for_qr` | `sidecar_qr`: while true, the QR image is re-fetched every 5 s; once `connected`, the console calls `connect/finish` once |
| `push_name`, `account`, `login`, `email`, `jid` | the "connected as" line (first one present) |
| `installed_permissions` (object; `permissions` also read) | `github_app`: the installed permission set |
| `mode`, `repository_selection`, `repositories_count` | `github_app`: App or PAT mode, and which repositories the installation covers |
| `granted_scopes`, `missing_scopes` (lists) | `google_oauth`; missing scopes show as "reconnect needed" once connected |
| `<secret field>_set` (boolean) | secret config fields show "set" / "not set" and "Set" / "Replace"; without it they say only "stored in the plugin" |

For `github_app` and `google_oauth`, `connect/start` must answer an `https:`
`url`. The console rejects anything else with a message, and never opens it.

### Adding a plugin from its repository

The opt-in `aab-installer` installs external plugins
(`docs/plugin-packaging.md`). The console drives it through the broker
(`services/plugin_install.py`), and the broker does the authority part.

- **+ Add plugin** (in the header of the Plugins view) first asks
  `GET /v1/admin/plugins/install/status`. When the installer is **off** (no
  `INSTALLER_URL` on the broker), the dialog does not offer Inspect. Instead,
  it shows these items:
  - The `.env` lines that turn the installer on: `INSTALLER_ENABLED=true` and
    `INSTALLER_ALLOWED_SOURCES=github.com/<you>/*`.
  - The one-time `docker login ghcr.io`.
  - The command that loads the overlay:
    `docker compose $(scripts/compose-files.sh) up -d`.
  - A note: the GitHub token for private repositories goes in this dialog
    after the installer is on, not in `.env`.

  Nothing in the console can turn the installer on, because the installer is
  root on the server.
- **GitHub token for private plugin repositories**, under the repository and
  ref fields. It is optional and write-only, like the Telegram bot token. It
  is a password field in its own small form, with **Set** and **Clear**. Set
  becomes Replace when the broker has a token. A badge shows the status's
  `git_token` state word: **not set**, **set**, or **re-enter required**.
  **re-enter required** means `unreadable`: the `BROKER_SECRETS_KEY` changed.
  Then the broker rejects inspect, install and upgrade until the owner enters
  the token again or clears it. On submit, the page first checks the broker's
  shape rule: 20 to 255 printable characters, no spaces. It reads the value
  once and empties the field before it sends the request. The value goes to
  `POST /v1/admin/plugins/install/git-token`. Nothing ever writes a token
  back. The script only compares `git_token` with the state words (tested).
  Clear asks first, then sends `DELETE /v1/admin/plugins/install/git-token`.
  Without `BROKER_SECRETS_KEY`, the page disables Set and shows the reason.
  The help line gives three facts:
  - Use a read-only token.
  - The broker stores it encrypted and never shows it again.
  - The broker sends it to the installer only for clones of `github.com`
    repositories on the installer's allowlist.
- Otherwise, the dialog takes a **repository** and a **release tag** or a
  full 40-character commit. The repository is like
  `github.com/you/aab-plugin-x`, and the dialog also accepts an https URL.
  The tag is like `v1.2.3`. The allowlist hint is under these fields.
  **Inspect** (`POST .../install/inspect`) has the installer clone and read
  the package. Then the dialog shows the **review card**, with these items:
  - The repository and the resolved commit.
  - The service: `plugin-<service>`, alone on `net_<service>` with the
    broker.
  - New install, or upgrade from which ref.
  - Per plugin: its display name and version, and every action with its side
    effect and modes.
  - The resources, narrowings and constraints.
  - The settings it will ask for. Secret ones carry a mark. The owner enters
    them later in the plugin's card, and the broker never keeps them.
  - On an upgrade, **what changes** against the current pin: actions,
    narrowings, constraints and settings added, removed or changed, new
    secrets, a changed connection.
  - Then **what the container gets**: its secret store and declared volumes,
    its literal environment, the server's `.env` keys that it can read, its
    Dockerfile and runtime line.

  The plugin's author wrote everything on the card, and the card shows it as
  text.
- In these cases the card has no Install button and says why:
  - A manifest that does not validate.
  - An id that the broker tree owns or another service serves.
  - A service that the installer already installed. Use Upgrade instead.
- **Install** (`POST /v1/admin/plugins/install`, or
  `POST /v1/admin/plugins/{service}/upgrade`) sends only the repository, the
  ref and the reviewed commit. Then the broker does these steps:
  1. It inspects again, and rejects the request if the ref moved.
  2. It **pins every manifest** (audited `plugin.pin`).
  3. It asks the installer for the job (`plugin.install` / `plugin.upgrade`).

  If the broker has a stored GitHub token, it adds the token to its own
  requests to the installer. The page never holds it.
- The **job panel** above the plugin cards polls `GET .../install/jobs/{id}`
  every 2 s. It shows the job's state and log lines: the steps, the compose
  commands, their exit codes and a short redacted tail of their output.
  Install and upgrade **recreate the broker**. While the broker does not
  answer, the panel keeps polling and says that the installer recreates the
  broker. No answer means no connection, or 502 / 503 / 504 from the edge.
  After five minutes without an answer, the panel stops and says that the job
  continues in the installer. The panel stops on `done` or `failed` and
  reloads the view. The broker finds a just-started service within 30
  seconds. The new plugin appears **disabled**. Enable it like any other
  plugin.
- An installed plugin's card shows **installed from `<source>@<ref>`** and the
  commit. It has **Upgrade** (the same dialog, with the repository fixed) and
  **Remove**. Remove asks for confirmation, names what the service serves, and
  has a purge checkbox:
  - Without purge, the service's volumes and its two `.env` secrets stay.
    Thus a new install of the service finds its data.
  - With purge, the installer deletes them for good.

  When the installer accepts the removal, the broker unpins what the service
  served. Thus agents get 404 for it at once. A package with no registered
  plugin (still starting, or awaiting review) appears under **Installed, not
  serving**, with the same buttons.
- **Offered, awaiting review** (`GET /v1/admin/plugins/offered`) replaces the
  old `Refused plugins` card. It lists every service that answered with a
  manifest that the broker has no approved copy of, or a different one. Where
  a pin can fix it, **Review and pin** opens the same review card for that
  manifest and pins it (`POST /v1/admin/plugins/{id}/pin`). Otherwise, the
  card shows the reason: an in-tree id, an id that another service serves, or
  an invalid manifest. This is also how the owner restores a plugin after a
  failed upgrade. The old version comes back offered, and a pin registers it
  again.

## Channels: Telegram

The Channels view reads `GET /v1/admin/telegram`. The answer has these
fields: `token` (`unset` | `set` | `unreadable`), `bot_username`, `enabled`,
`linked`, `chat_id`, `user_id`, `linking`, `active`,
`secrets_key_configured` and `poll`. `poll` holds running, last success, and
the error streak with the type and status of the last error.

- **Bot token, write-only.** The field is a password input in a form. On
  submit, the page reads the value once and empties the field before it sends
  the request. The value goes to `POST /v1/admin/telegram/token`. Nothing ever
  writes a token back. The status only says whether the broker has one, and
  the script only compares `token` with those three words (tested). The page
  also empties the field on navigation and sign-out. `unreadable` (the
  `BROKER_SECRETS_KEY` changed) shows **re-enter required**. Without
  `BROKER_SECRETS_KEY`, the page disables the Set button and shows the reason.
  Clear is `DELETE /v1/admin/telegram/token`. The chat link stays, so the
  token of the same bot resumes it.
- **Linking.** "Link my chat" calls `link/start` and shows `/start <code>`.
  When the broker returns a `t.me` deep link (https on `t.me` only), the page
  also shows an "Open the bot in Telegram" button. The code binds whichever
  Telegram account sends it first. So the page says this, never keeps the
  code, and drops it on navigation. The page polls the status every 3 s and
  updates a countdown every second. It stops when the link completes (toast,
  then Enable), when the code expires, or when the owner leaves the view.
- **Linked:** chat and user ids, Enable / Disable, Send a test message, Unlink
  (with confirmation). `active` (token stored, linked and enabled) drives the
  "on" badge and the header pill.

## Settings

The Settings view reads `GET /v1/admin/settings`. It shows every entry of
`settings[]` with these items:

- Its help text, default, bounds and unit.
- The source of the current value: set here, the environment file, or the
  built-in default.

There is one input per setting type (`int`, `float`, `hosts`). A test keeps
this equal to `runtime_settings.SPECS`. Durations also show in hours or days.
Save sends `PATCH /v1/admin/settings` with only the settings that changed.
"Reset to default" sends `null`, which returns the setting to the file
default. The view shows that button for a setting set here. The page checks
values against their bounds before it sends them, and the broker checks again.

`mcp_allowed_hosts_extra` shows the hostnames from the file, which the owner
cannot remove here. It also shows the effective list, and a note that it
**applies at the next broker start**. The reason is that the `/mcp` transport
reads its `Host` allowlist when it starts.

"What lives in files" lists `env_only[]`. For each key, it shows the key, its
category (bootstrap, exposure, deployment, compose) and its state. For a
secret, the state is whether it has a value. For the rest, it is the value.
The panel also says why the console cannot edit the key. It says plainly that
the console deliberately cannot edit these keys, for two reasons:

- Some keys must exist before the database is readable.
- The other keys decide exposure and fail closed at boot, so a hijacked
  session must not be able to weaken them.

## Delegations

The Delegations view reads `GET /v1/admin/keys/tree`: root keys with their
delegated children nested. Each node shows these items:

- Its name.
- `status` (active, disabled, expired).
- `live`.
- `depth`.
- Its ceiling (role), when lower than `full`.
- `orphan`, when its chain is broken.
- Its creation time and last use.
- A grants summary: counts by status, and which plugins and how many actions
  the active grants reach.
- The capabilities of its active and pending grants, in the same readable form
  as permission requests.

A key that is not live says why:

- The key itself has the status disabled or expired.
- A key above it has the status disabled or expired.
- Its chain is broken.
- It is deeper than `max_delegation_depth`.

The view has these controls:

- **Revoke** (delegated keys): the same effect as an agent's
  `revoke_delegation`. The console first disables the key, so the key and
  every key below it stop authenticating at once. Then it revokes the active
  and pending grants of the key one by one. It skips a grant that another path
  already decided (409). The confirmation says how many keys below it also
  stop.
- **Disable / Enable** any key, and **Edit** (the key dialog).
- Expand / collapse per node or all at once. `#/delegations/<key id>` (the
  "Tree" link of the Keys view) expands the path to that key and highlights
  it.

## Security design

- **Nonce CSP.** Every response has a fresh nonce. Its CSP is
  `default-src 'self'; script-src 'nonce-...'; style-src 'self' 'unsafe-inline';
  img-src 'self' data:; connect-src 'self'; frame-ancestors 'none';
  base-uri 'none'; form-action 'none'; object-src 'none'`. The response also
  carries `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`,
  `X-Frame-Options: DENY` and `Referrer-Policy: no-referrer`. The console
  shows text that agents control (notes, params, labels, delegated key names)
  to the person who approves those agents. If an escaping slip ever lets
  markup through, the nonce policy still does not run it. Thus the slip cannot
  become "the agent approves its own request". `form-action 'none'` means that
  a form whose script failed can never fall back to a native submit that puts
  a password in a URL.
- **No HTML-string sinks.** The page builds all DOM with `h()` (attributes
  through `setAttribute`, text through text nodes) and `textContent`. The file
  contains no `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `document.write`,
  `eval` or inline event-handler attributes. Tests enforce all of it.
- **The page marks agent text as such.** Agent text sits in a quoted,
  bidi-isolated box. The page shows Unicode embedding, override and isolate
  controls (U+202A-202E, U+2066-2069) as visible `[U+202E]` markers. Thus an
  override cannot reorder a summary or a file name on an approval card. The
  page leaves LRM/RLM alone, because Hebrew and Arabic names use them.
- **One-time secrets.** The console shows a new agent key, a rotated key or a
  new admin token once, in a copy box, and nowhere else. It clears the
  admin-token banner on navigation. Secret config fields are write-only. The
  console sends the value once to the plugin, through the broker, which keeps
  none of it. It clears the value from the input immediately, whether or not
  the call succeeds.
- **Links from plugins** (GitHub install, Google consent) must be `https:`. A
  Telegram deep link must be https on `t.me`. They open with
  `rel="noopener noreferrer"`.
- **The Telegram bot token** is write-only end to end (see Channels). The
  console never keeps the one-time link code.
- **Confirmation** comes before every destructive or disruptive step:
  - Disable a plugin.
  - Disconnect.
  - Remove an installed plugin.
  - Disable, rotate or revoke a key.
  - Revoke a grant, token or session.
  - Unhide.
  - Cancel a scheduled action.
  - Clear a secret or the bot token.
  - Unlink Telegram.

  An install or a pin always starts with a review card.
- **Plugin-written text.** An external plugin's manifest and descriptor
  (names, descriptions, action docs, paths, environment values) come from its
  repository. The review card shows all of it as text through `h()`, like
  agent text. Dialogs focus their safe button. Dialogs that hold a form close
  only through their buttons.

## Changing the console

- Add a view as a `section class="view" data-view="..."`, plus a nav link and
  an entry in `VIEWS`. Put its loader in `VIEW_REFRESH`; a test requires one
  per view. Put it in `LIVE_VIEWS` only if it has no form. Timers that belong
  to a view stop in `stopBackground()` (navigation and sign-out).
- `Element.append()` prints a `null` argument as the text "null". Pass parts
  that can be absent through `h()`, which drops them.
- Build DOM with `h()`. Assign handlers in script (`el.onclick = ...`).
- Write API paths as literals that begin with the path (`"/v1/admin/..."` or a
  template literal `` `/v1/admin/keys/${enc(id)}` ``). The test extracts every
  `/v1/...` and `/auth/...` literal and checks it against the app's OpenAPI
  path table. Do not name a path in a comment unless the path exists.
- Send everything through `api()` (CSRF header, 401 handling, error text).

`broker/tests/test_console.py` covers these items:

- Headers and nonce.
- An identical page for every `/admin` path.
- No owner credential needed, and no data in the page.
- Cloudflare Access applies.
- No external URLs.
- No HTML sinks.
- No inline handlers.
- No hidden bidi controls in the source.
- The CSRF constant.
- The role list.
- The ceiling presentation:
  - The "Ceiling (role)" label in both key dialogs.
  - The help line.
  - The `full` default.
  - The effective-mode table, equal to `roles.role_caps`.
  - `effectiveMode()`, which follows the broker's rule.
  - The keys list and tree, which show the ceiling only below `full`.
  - The grant card's ceiling note.
- Form vocabulary.
- A config renderer per manifest field type.
- A connection panel per connection kind.
- A settings input per setting type.
- One loader per view, and no placeholder left.
- The Telegram token field is write-only: markup, no write-back, cleared
  before sending, status only compared.
- The shared-slot markers.
- Every API path exists.
- The manifest projection.

For + Add plugin, the test covers these items:

- The header button and panels.
- The offered card.
- Provenance and remove.
- The installer-off dialog. Its fallbacks equal the broker's.
- Inspect before install, with only the reviewed commit sent.
- The job panel's interval, restart statuses and stop states.

Where node is present, the whole script must parse (`node --check`). Also, the
test runs the page's own review functions under node on the broker's real
review data. They must show every fact the owner approves.
