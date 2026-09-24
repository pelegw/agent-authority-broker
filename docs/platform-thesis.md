# Platform thesis: what the second and third plugins cost

The brief (section 4) predicts that a target plugin splits into a commodity
layer (transport, action inventory) that generalizes, and an authority model
that does not. The test it proposes: encode two deliberately different systems
against the same declarative schema and measure how much the second reuses.
The plan's metric is blunter: adding a plugin must touch zero engine files.

This document reports the measurement for the three plugins in 0.2.0. All
numbers come from `git diff --stat` between each lane's base commit and its
first commit, before any merge of `dev`, so they show exactly what each plugin
cost the platform.

## Engine files touched

| Plugin | Engine, policy, registry, authority, hidden, MCP, skill, console files touched |
|---|---|
| WhatsApp (first) | 0 |
| GitHub (second) | 0 |
| Google: Gmail, Calendar, Drive (third, one service) | 0 |

The metric holds. The policy engine, the grant algebra, the registry, the
visibility layer, the MCP surface and the skill generator were not edited to
add any plugin.

## Everything else touched outside the plugin's own directory

**GitHub** (5 files): the vendored manifest copy and its package docstring,
its compose block, its CI job, one docs index row. Nothing generic changed.

**Google** (20 files). Besides the same four kinds of housekeeping (three
vendored manifests, compose block, CI job, docs row), the lane needed two
generic platform features that neither WhatsApp nor GitHub had exercised:

| Feature | Files | Why Google needed it |
|---|---|---|
| Shared config fields across the plugin ids one service hosts (`config_schema[].shared`; the runtime writes them once to the connection's slot) | `plugins/manifest.py` (+11), `plugins/settings.py` (+23), `services/plugins_admin.py` (part of +71), `plugin-runtime/app.py` (+76), runtime tests | Gmail, Calendar and Drive share one OAuth client secret; storing it per plugin id would mean entering it three times and keeping three copies. |
| Broker-computed OAuth `redirect_uri` passed in `/connect/start` | `plugins/adapter.py` (+31), `routers/admin_plugins.py` (+9), `services/plugins_admin.py`, `plugin-runtime/app.py`, `docs/plugin-api.md` | The plugin container does not know the broker's public hostname or port; the broker does, and in public mode it must come from the environment so a hijacked session cannot redirect the consent flow. |

Both are properties of "a service hosting several plugin ids over one OAuth
credential", not of Google. A future Microsoft 365 service (Outlook, Calendar,
OneDrive) would use both unchanged. Counting them as platform work rather than
plugin work is fair, and it is the honest answer to the brief's "does the third
integration cost less than the second": the third cost more, because it was the
first of a new shape, and the platform now has that shape.

## Code and manifest sizes

| Plugin | Python lines | Manifests | Manifest lines |
|---|---|---|---|
| WhatsApp | 1,037 | 1 | 164 |
| GitHub | 1,950 | 1 | 300 |
| Google (three plugin ids) | 2,491 | 3 | 273 + 235 + 252 |

Inside the Google package, 1,062 lines are shared by all three adapters
(connection and token minting, HTTP client, CallScope parsing, id
normalization, scope tables) and 1,422 are adapter-specific (Gmail 589,
Calendar 390, Drive 443). The marginal cost of the third Google product was
therefore roughly 400 to 600 lines plus a 250-line manifest.

Between GitHub and Google, no adapter code is shared. That matches the brief's
prediction that transport is the commodity layer: each is generated from its
target's API shape and is cheap to write, but there is nothing to share.
What the two do share is everything above transport: the plugin API contract,
the runtime, the CallScope shape, the visibility rules, the 503/502 error
contract, and the manifest schema.

## Manifest schema features the third plugin needed

The schema was written for the plan's WhatsApp and GitHub sketches. Google was
the first to use these parts of it:

- `config_schema[].shared` (added by the lane) and the first real use of `connection.shared`.
- Scalar `constraints` of all three forms: range (`date_window_days`, `time_window_days`, `max_download_mb`), flag (`attachments`, `bcc`, `private_events`, `file_content`, ...), level (`visibility: freebusy | full`). WhatsApp and GitHub declare none.
- `pattern` narrowings with no resource kind behind them (Gmail `domain`, Calendar `attendee`).
- A real `subtree` narrowing (Drive `folder`) backed by the plugin's `ancestors` lookup.
- `target_permissions` holding several OAuth scopes for one action, with a derived `scopes` narrowing.
- Array params (`to`, `cc`, `bcc`, `attendees`).
- Normalizers that resolve aliases through the target API (`primary`, `root`, label names), so an alias can never be a second name for a hidden resource.

None of these required a schema change except `shared`. The lattice
vocabulary (list, subtree, pattern, range, flag, level) covered every Google
narrowing the plan asked for, with one exception: `hide_keyword` is a
deny-list, which an allow-list lattice cannot express; it was dropped, and
owners hide events by id instead.

## What did not generalize

As the brief predicted, the authority model is bespoke per system:

| Concern | WhatsApp | GitHub | Google |
|---|---|---|---|
| Resource vocabulary | chat, contact | repo, branch | label, thread, contact, calendar, event, folder, file |
| Unit of `target_permissions` | none (proxy-only) | App permission names (`contents: read`) | OAuth scope URLs |
| Credential minting | sidecar session (plaintext, volume-scoped) | installation token per (repos, permissions) tuple | refresh with a `scope` subset, cached per scope set |
| Connect flow | QR pairing | App install + installation id | OAuth consent + code exchange |
| Where narrowing is enforced | proxy only | repo and permissions by GitHub; branch by proxy | read/write split by Google; everything else by proxy |

Each of these is data in the manifest or code in the connection object; the
engine reads the manifest and asks the connection for a credential that
matches the call's requirements. That boundary is what kept the engine count
at zero.

## A rule the algebra taught us

The Google lane found that `capability._is_top` treats a flag's `true` as
"unrestricted" and drops it at normalization. A flag whose `true` side
restricts would therefore vanish and fail open. The rule, now documented in
`docs/manifest-schema.md` and enforced by a plugin-side lint: **for a flag,
`true` is always the permissive side; name flags for the permission they
grant** (`private_events`, `others_events`, `file_content`, never
`hide_private` or `metadata_only`). It is the kind of defect the brief's
certification gate exists to catch, and the kind a declarative spec makes
visible in review.

## Verdict

- Engine files touched by the second and third plugins: zero. The thesis holds where it was stated.
- The third plugin cost more platform work than the second, because it introduced a new shape (one service, several plugin ids, one shared credential). That shape is now free for the next service of its kind.
- Transport does not compound and was never expected to. The manifest vocabulary compounds: Google needed one new field.
- The measurement to keep taking, per the brief's section 5.2: for the next plugin, count the files outside its directory again. A rising count means the platform is leaking; a flat count of five means it is real.
