# Plugin manifest schema

Every target plugin ships a `manifest.yaml`. The broker keeps a vendored copy
at `broker/broker/targets/<id>/manifest.yaml`. The manifest is the single
description of the target:

- The grant lattice (narrowings and constraints).
- The actions and their parameters.
- The text that the skill doc and the console show.

The broker makes the tools, REST routes, Telegram cards, the console
capability editor and the skill doc from it. Thus adding a plugin touches no
engine file.

Loader: `broker.plugins.manifest.load_manifest(path)` /
`load_manifest_text(text)`. Both return a validated, frozen `Manifest` or raise
`ManifestError`. Validation is strict: unknown keys are errors. Anything that
the broker does not actually enforce fails loudly, and the loader never ignores
it.

Worked examples: `broker/broker/targets/whatsapp/manifest.yaml` (full),
`broker/broker/targets/github/manifest.yaml`, and the test plugin
`broker/tests/fixtures/echo/manifest.yaml`, which uses every form.

## Top level

| key | type | notes |
|---|---|---|
| `id` | string | `^[a-z][a-z0-9]*$`. No underscore, so the MCP tool name `<id>_<action>` splits back unambiguously. |
| `version` | string | `MAJOR.MINOR.PATCH`. Phase 3 checks a running plugin's manifest id+version against the vendored copy. |
| `display_name` | string | |
| `description` | string | optional |
| `connection` | object | see below |
| `config_schema` | list | console config form |
| `resources` | map kind -> resource | |
| `narrowings` | list | grant dimensions |
| `constraints` | list | scalar limits |
| `actions` | list | at least one |
| `skill` | object | skill-doc text |

### `connection`

| key | values | notes |
|---|---|---|
| `kind` | `sidecar_qr` \| `github_app` \| `google_oauth` \| `none` | `none` is for in-process plugins holding no credential (the `echo` test plugin). |
| `shared` | string or null | shared credential slot, for example `google` for gmail/gcal/gdrive |
| `enforcement` | `target` \| `proxy` | default `proxy` |

### `config_schema[]`

A config field has the keys
`{name, type, secret, shared, required, default, help, values}`. `type` is
one of `string | text | integer | boolean | enum`. Rules:

- Names match `^[a-z][a-z0-9_]*$` and are unique.
- `enum` needs `values`, and only `enum` has them.
- A `secret` field has no default.
- A default must match the type.

`shared: true` marks a field that belongs to the shared slot of the
connection (`connection.shared`), not to this plugin alone. An example is the
Google OAuth client id and secret behind gmail, gcal and gdrive. For such
fields:

- The console shows them once per slot.
- The broker keeps a shared non-secret value identical on every plugin of the
  slot.
- The plugin runtime stores a shared secret once, in that slot.

The loader rejects a manifest with a shared field but no `connection.shared`.

### `resources`

`{kind: {display, normalize, resolve, hideable, id_format}}`. `normalize` names
the adapter normalizer, for example `jid`. `resolve` says that the adapter can
map names to ids. `hideable` lets the owner hide instances (hidden == 404).
Kinds match `^[a-z][a-z0-9_]*$`.

### `narrowings[]`

A narrowing has the keys
`{dimension, form, applies_to, enforcement, derived_from, values, resource, doc}`.

- `form`: one of the six forms of docs/grant-algebra.md. The set-valued forms
  `list`, `subtree` and `pattern` go in the `selector` of a capability. The
  scalar forms `range`, `flag` and `level` go in `constraints`.
- `applies_to`: action names, or `["*"]` for all. Every name must exist.
- `enforcement`: `target` or `proxy`. With `target`, the target system itself
  enforces the narrowing, for example a GitHub installation token limited to
  the listed repositories. With `proxy`, the broker filters. The broker
  reports the value per call in `enforced_where`.
- `derived_from: target_permissions`: the broker calculates the dimension
  from the `target_permissions` of the permitted actions (GitHub
  `permissions`, Google `scopes`). A grant never stores such a dimension and
  cannot set it. `normalize()` rejects them.
- `values`: required for `level` (>= 2 unique, ordered low -> high),
  forbidden otherwise.
- `resource`: the resource kind that the ids belong to. Required for
  `subtree`, because the broker walks ancestry per kind. The broker also uses
  it to match per-key denies.

### `constraints[]`

A constraint has the keys
`{name, form, applies_to, default, enforcement, values, doc}`. Forms are
scalar only: `range` (integer >= 0), `flag` (boolean), `level` (with
`values`). A set-valued rule is either an allowlist or a deny. Declare an
allowlist as a narrowing. A deny belongs in denies, outside the lattice.

**WARNING: Name a flag for the permission it grants. `true` must be the
permissive side.** The algebra treats an absent flag as `true`. It treats
`true` as top and drops it at normalization (docs/grant-algebra.md). Consider
a flag whose `true` *restricts*, for example `hide_private`, `metadata_only`
or `own_events_only`. Normalization drops it from every grant, and the flag
fails open. Write `private_events`, `file_content` and `others_events`
instead, and restrict with `false`. The same rule applies to a `level`: its
last value is top. Order the values from the most restrictive to the most
permissive. The tests of the Google plugins lint their flag names for this.

`default` is what the console pre-fills when the owner builds a capability.
In the algebra, an **absent** constraint means unrestricted. The broker does
not apply the default implicitly.

Names of narrowings and constraints share one namespace, so each name is
unique across both. A name cannot be one of the reserved words `mode`,
`budget`, `target`, `actions`, `selector`, `constraints`, `expires_at`.

### Built-in dimensions

- `mode`: level `draft < direct` on every action. Reads are always direct.
- `budget`: `{per_minute?, per_day?}` on every capability; absent = unlimited.

A manifest declares neither of them.

### `actions[]`

| key | notes |
|---|---|
| `name` | `^[a-z][a-z0-9_]*$`, unique; cannot be `*`, `read_*`, `write_*`, `destructive_*` |
| `side_effect` | `read` \| `write` \| `destructive` |
| `resource` | resource kind this action addresses (must exist) |
| `selector_param` | param holding the resource id; must be a declared param, needs `resource` |
| `params` | JSON-schema subset, below |
| `modes` | subset of `[direct, draft]`, non-empty, unique. Default `[direct]` for reads, `[direct, draft]` for writes. Reads can only be `[direct]`. |
| `schedulable` | writes only |
| `long_poll` | reads only |
| `returns` | `json` (default) \| `binary` |
| `target_permissions` | `{unit: level}`, interpreted by the connection kind (GitHub App permission names, Google OAuth scopes) |
| `summary_template` | `str.format` template for approval cards; placeholders must be plain param names or `<param>_label` |
| `doc` | one-line description |

The loader accepts action-set sugar on input but never stores it. The sugar
is `*` (all actions), and `read_*`, `write_*` and `destructive_*` (all
actions of that side effect). Any other glob, or an unknown name, is an
error.

### `params`: the JSON-schema subset

The top level must be `type: object`. The loader supports these keywords:

| keyword | allowed on |
|---|---|
| `type` | `object`, `string`, `integer`, `boolean`, `array` |
| `properties`, `required` | object |
| `minLength`, `maxLength` | string |
| `minimum`, `maximum` | integer |
| `items` | array (required) |
| `enum` | string, integer (non-empty, values of that type) |
| `default` | any non-object; validated against the field's own rules |
| `description` | any |

Anything else (`pattern`, `format`, `number`, `oneOf`, `additionalProperties`,
...) raises. A required param cannot have a default. `required` names must be
in `properties`. Property names are identifiers that do not start with `_`.

At load, the params of each action become a pydantic model
(`manifest.action(name).params_model`). The model is **strict** (no `"5"` ->
`5` coercion) and **forbids unknown params**.

### `skill`

`{addressing, rules[], examples[{title, action, params}]}`. The action of each
example must exist, and its params must validate against the model of the
action.

`broker/broker/skill/` writes this into the section of the plugin in the
agent skill doc. It copies `addressing` and `rules` verbatim, and it writes
each example as a REST `curl` call. The rest of the section comes from the
other manifest fields:

- The resources and dimensions.
- The action table, with params, modes and schedulability.
- The place that enforces each limit.

The copy for a key (`/v1/me/skill`) drops the actions and examples that the
key cannot reach.
