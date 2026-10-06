# Platform thesis: what the second and third plugins cost

The brief (section 4) predicts that a target plugin splits into two layers. A
commodity layer (transport, action inventory) generalizes. The authority
model does not. The brief proposes a test: encode two systems that are
different on purpose against the same declarative schema. Then measure how
much the second one reuses. The plan's metric is simpler: adding a plugin
must touch zero engine files.

This document reports the measurement for the three plugins in 0.2.0. All
numbers come from `git diff --stat` between the base commit of each lane and
its first commit, before any merge of `dev`. Thus they show exactly what each
plugin cost the platform.

## Engine files touched

| Plugin | Engine, policy, registry, authority, hidden, MCP, skill, console files touched |
|---|---|
| WhatsApp (first) | 0 |
| GitHub (second) | 0 |
| Google: Gmail, Calendar, Drive (third, one service) | 0 |

The metric holds. No plugin needed an edit to the policy engine, the grant
algebra, the registry, the visibility layer, the MCP surface or the skill
generator.

## Everything else touched outside the plugin's own directory

**GitHub** (5 files): the vendored manifest copy and its package docstring,
its compose block, its CI job and one docs index row. Nothing generic
changed.

**Google** (20 files). The lane had the same four kinds of housekeeping:
three vendored manifests, a compose block, a CI job and a docs row. It also
needed two generic platform features that neither WhatsApp nor GitHub had
used. This table lists them.

| Feature | Files | Why Google needed it |
|---|---|---|
| Shared config fields across the plugin ids one service hosts (`config_schema[].shared`; the runtime writes them once to the connection's slot) | `plugins/manifest.py` (+11), `plugins/settings.py` (+23), `services/plugins_admin.py` (part of +71), `plugin-runtime/app.py` (+76), runtime tests | Gmail, Calendar and Drive share one OAuth client secret; storing it per plugin id would mean entering it three times and keeping three copies. |
| Broker-computed OAuth `redirect_uri` passed in `/connect/start` | `plugins/adapter.py` (+31), `routers/admin_plugins.py` (+9), `services/plugins_admin.py`, `plugin-runtime/app.py`, `docs/plugin-api.md` | The plugin container does not know the broker's public hostname or port; the broker does, and in public mode it must come from the environment so a hijacked session cannot redirect the consent flow. |

Both are properties of "a service with several plugin ids over one OAuth
credential", not of Google. A future Microsoft 365 service (Outlook,
Calendar, OneDrive) can use both unchanged. Thus it is fair to count them as
platform work, not plugin work. The brief asks: "does the third integration
cost less than the second". The honest answer is no. The third cost more,
because it was the first of a new shape. The platform now has that shape.

## Code and manifest sizes

| Plugin | Python lines | Manifests | Manifest lines |
|---|---|---|---|
| WhatsApp | 1,037 | 1 | 164 |
| GitHub | 1,950 | 1 | 300 |
| Google (three plugin ids) | 2,491 | 3 | 273 + 235 + 252 |

Inside the Google package, all three adapters share 1,062 lines: connection
and token minting, HTTP client, CallScope parsing, id normalization and
scope tables. The other 1,422 lines are adapter-specific (Gmail 589,
Calendar 390, Drive 443). Thus the marginal cost of the third Google product
was about 400 to 600 lines plus a 250-line manifest.

GitHub and Google share no adapter code. That matches the brief's prediction
that transport is the commodity layer. Each adapter follows the API shape of
its target and is cheap to write, but there is nothing to share. The two
share everything above transport:

- The plugin API contract.
- The runtime.
- The CallScope shape.
- The visibility rules.
- The 503/502 error contract.
- The manifest schema.

## Manifest schema features the third plugin needed

The schema came from the WhatsApp and GitHub sketches in the plan. Google
was the first plugin to use these parts of it:

- `config_schema[].shared` (the lane added it) and the first real use of
  `connection.shared`.
- Scalar `constraints` of all three forms: range (`date_window_days`,
  `time_window_days`, `max_download_mb`), flag (`attachments`, `bcc`,
  `private_events`, `file_content`, ...), level (`visibility: freebusy | full`).
  WhatsApp and GitHub declare none.
- `pattern` narrowings with no resource kind behind them (Gmail `domain`,
  Calendar `attendee`).
- A real `subtree` narrowing (Drive `folder`) on top of the plugin's
  `ancestors` lookup.
- `target_permissions` that hold several OAuth scopes for one action, with a
  `scopes` narrowing calculated from them.
- Array params (`to`, `cc`, `bcc`, `attendees`).
- Normalizers that resolve aliases through the target API (`primary`,
  `root`, label names), so an alias can never be a second name for a hidden
  resource.

Only `shared` needed a schema change. The lattice vocabulary (list, subtree,
pattern, range, flag, level) covered every Google narrowing in the plan,
with one exception. `hide_keyword` is a denylist, and an allowlist lattice
cannot express it. The lane dropped it. Owners hide events by id instead.

## What did not generalize

As the brief predicted, the authority model is specific to each system.

| Concern | WhatsApp | GitHub | Google |
|---|---|---|---|
| Resource vocabulary | chat, contact | repo, branch | label, thread, contact, calendar, event, folder, file |
| Unit of `target_permissions` | none (proxy-only) | App permission names (`contents: read`) | OAuth scope URLs |
| Credential minting | sidecar session (plaintext, volume-scoped) | installation token per (repos, permissions) tuple | refresh with a `scope` subset, cached per scope set |
| Connect flow | QR pairing | App install + installation id | OAuth consent + code exchange |
| Where narrowing is enforced | proxy only | repo and permissions by GitHub; branch by proxy | read/write split by Google; everything else by proxy |

Each of these is data in the manifest or code in the connection object. The
engine reads the manifest. It asks the connection for a credential that
matches the requirements of the call. That boundary kept the engine count at
zero.

## A rule the algebra taught us

The Google lane found that `capability._is_top` treats a flag's `true` as
"unrestricted". It drops that value at normalization. Thus a flag whose
`true` side restricts disappears, and its limit fails open.
`docs/manifest-schema.md` now documents the rule, and a plugin-side lint
enforces it. **For a flag, `true` is always the permissive side. Name flags
for the permission they grant** (`private_events`, `others_events`,
`file_content`, never `hide_private` or `metadata_only`). The brief's
certification gate exists to catch this kind of defect. A declarative spec
makes this kind of defect visible in review.

## What external packaging cost

Release 0.3.0 moved a plugin out of the gateway's tree (for the finance
plugin). This was the first change that had to touch the trust root of the
registry, not the manifest vocabulary. It still touched zero engine files.
These parts did not change (`git diff --stat` against the 0.2.0 line,
measured before the merge):

- The policy engine.
- The grant algebra.
- The decision record.
- The ledger.
- The visibility layer.
- The MCP surface.
- The skill generator.

The cost went to other places:

- The pin moved from a vendored file to a table that the owner writes. The
  change is in `plugins/pins.py`, the registry's fall-through and its list
  of offers awaiting review.
- An owner API and console flow to review, pin, install, upgrade and
  remove. That is about 770 lines in `pins.py`, `services/plugin_install.py`
  and `routers/admin_install.py`, plus the Plugins view.
- A new container with its own package (`installer/`). It has about 1,800
  lines of code besides the shared logging copies, and as many lines of
  tests.
- A compose file set that a script calculates, not a fixed list
  (`scripts/compose-files.sh`). `deploy/push.sh`, the installer and the docs
  use it.
- A published runtime: the base image and the wheel. The release workflow
  builds them on every tag.

The finance plugin itself touched no gateway file.

The real price is a new trust boundary: the installer is root on the
machine. Structure limits it, not policy checks:

- Its own network, with the broker alone.
- A token.
- An env-only allowlist.
- Overlays that the installer makes from a validated descriptor through a
  fixed template.
- A pin on the reviewed commit.
- A git credential that only `GIT_ASKPASS` can reach.

The engine makes the same choice.

## Verdict

- Engine files touched by the second and third plugins: zero. The thesis holds where the brief stated it.
- The third plugin cost more platform work than the second, because it introduced a new shape (one service, several plugin ids, one shared credential). That shape is now free for the next service of its kind.
- Nobody expected transport to compound, and it does not. The manifest vocabulary compounds: Google needed one new field.
- Continue the measurement, as the brief's section 5.2 says. For the next plugin, count the files outside its directory again. A rising count means that the platform leaks. A flat count of five means that it is real.
