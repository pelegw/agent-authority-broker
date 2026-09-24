# Plugin manifest schema

Every target plugin ships a `manifest.yaml` (vendored at
`broker/broker/targets/<id>/manifest.yaml`). It is the single description of
the target: the grant lattice (narrowings and constraints), the actions and
their parameters, and the text the skill doc and console show. Tools, REST
routes, Telegram cards, the console capability editor and the skill doc are
all derived from it, so adding a plugin touches no engine file.

Loader: `broker.plugins.manifest.load_manifest(path)` /
`load_manifest_text(text)`. Both return a validated, frozen `Manifest` or raise
`ManifestError`. Validation is strict: unknown keys are errors, and anything
the broker would not actually enforce fails loudly instead of being ignored.

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
| `shared` | string or null | shared credential slot, e.g. `google` for gmail/gcal/gdrive |
| `enforcement` | `target` \| `proxy` | default `proxy` |

### `config_schema[]`

`{name, type, secret, shared, required, default, help, values}`; `type` is one of
`string | text | integer | boolean | enum`. Rules: names match
`^[a-z][a-z0-9_]*$` and are unique; `enum` needs `values` (and only `enum` has
them); a `secret` field has no default; a default must match the type.

`shared: true` marks a field that belongs to the connection's shared slot
(`connection.shared`), not to this plugin alone: the Google OAuth client id
and secret behind gmail, gcal and gdrive. The console shows such fields once
per slot, the broker keeps a shared non-secret value identical on every
plugin of the slot, and the plugin runtime stores a shared secret once, in
that slot. A manifest with a shared field but no `connection.shared` is
refused.

### `resources`

`{kind: {display, normalize, resolve, hideable, id_format}}`. `normalize` names
the adapter normalizer (e.g. `jid`); `resolve` says the adapter can map names
to ids; `hideable` lets the owner hide instances (hidden == 404). Kinds match
`^[a-z][a-z0-9_]*$`.

### `narrowings[]`

`{dimension, form, applies_to, enforcement, derived_from, values, resource, doc}`

- `form`: one of the six forms of docs/grant-algebra.md: `list`, `subtree`,
  `pattern` (set-valued, stored in a capability's `selector`) or `range`,
  `flag`, `level` (scalar, stored in `constraints`).
- `applies_to`: action names, or `["*"]` for all. Every name must exist.
- `enforcement`: `target` (the target system itself enforces it, e.g. a
  GitHub installation token limited to the listed repositories) or `proxy`
  (the broker filters). Reported per call in `enforced_where`.
- `derived_from: target_permissions`: the dimension is computed from the
  allowed actions' `target_permissions` (GitHub `permissions`, Google
  `scopes`). Derived dimensions are never stored in a grant and cannot be
  set by one; `normalize()` rejects them.
- `values`: required for `level` (>= 2 unique, ordered low -> high),
  forbidden otherwise.
- `resource`: the resource kind the ids belong to. Required for `subtree`
  (ancestry is walked per kind); also used to match per-key denies.

### `constraints[]`

`{name, form, applies_to, default, enforcement, values, doc}`. Forms are
scalar only: `range` (integer >= 0), `flag` (boolean), `level` (with
`values`). A set-valued rule is either an allow-list (declare it as a
narrowing) or a deny (it belongs in denies, outside the lattice).

**Name a flag for the permission it grants: `true` must be the permissive
side.** The algebra treats an absent flag as `true`, treats `true` as top
and drops it at normalization (docs/grant-algebra.md). A flag whose `true`
*restricts* (`hide_private`, `metadata_only`, `own_events_only`) would
therefore vanish from every grant and fail open. Write `private_events`,
`file_content`, `others_events` instead, restricting with `false`. The same
holds for a `level`: its last value is top, so order values from most
restrictive to most permissive. The Google plugins' tests lint their flag
names for this.

`default` is what the console pre-fills when the owner builds a capability. In
the algebra an **absent** constraint means unrestricted; the default is not
applied implicitly.

Names of narrowings and constraints share one namespace (unique across both)
and cannot be the reserved words `mode`, `budget`, `target`, `actions`,
`selector`, `constraints`, `expires_at`.

### Built-in dimensions

- `mode`: level `draft < direct` on every action. Reads are always direct.
- `budget`: `{per_minute?, per_day?}` on every capability; absent = unlimited.

Neither is declared in a manifest.

### `actions[]`

| key | notes |
|---|---|
| `name` | `^[a-z][a-z0-9_]*$`, unique; cannot be `*`, `read_*`, `write_*`, `destructive_*` |
| `side_effect` | `read` \| `write` \| `destructive` |
| `resource` | resource kind this action addresses (must exist) |
| `selector_param` | param holding the resource id; must be a declared param, needs `resource` |
| `params` | JSON-schema subset, below |
| `modes` | subset of `[direct, draft]`, non-empty, unique. Default `[direct]` for reads, `[direct, draft]` for writes. Reads may only be `[direct]`. |
| `schedulable` | writes only |
| `long_poll` | reads only |
| `returns` | `json` (default) \| `binary` |
| `target_permissions` | `{unit: level}`, interpreted by the connection kind (GitHub App permission names, Google OAuth scopes) |
| `summary_template` | `str.format` template for approval cards; placeholders must be plain param names or `<param>_label` |
| `doc` | one-line description |

Action-set sugar accepted on input (never stored): `*` (all actions),
`read_*`, `write_*`, `destructive_*` (all actions of that side effect). Any
other glob, or an unknown name, is an error.

### `params`: the JSON-schema subset

Top level must be `type: object`. Supported keywords:

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
...) raises. A required param cannot have a default; `required` names must be
in `properties`; property names are identifiers not starting with `_`.

At load, each action's params become a pydantic model
(`manifest.action(name).params_model`) that is **strict** (no `"5"` -> `5`
coercion) and **forbids unknown params**.

### `skill`

`{addressing, rules[], examples[{title, action, params}]}`. Each example's
action must exist and its params must validate against the action's model.

`broker/broker/skill/` renders this into the plugin's section of the agent
skill doc: `addressing` and `rules` verbatim, each example as a REST `curl`
call. The rest of the section (resources, dimensions, the action table with
params, modes and schedulability, where each limit is enforced) comes from
the other manifest fields. A key's copy (`/v1/me/skill`) drops actions and
examples the key cannot reach.
