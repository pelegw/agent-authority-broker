"""The plugin manifest: everything the broker knows about a target, as data.

A manifest declares a target's resources, the narrowing dimensions a grant
may restrict, its constraints, and its actions (with a params schema). Tools,
REST routes, Telegram cards, the console capability editor and the skill doc
are all derived from it, which is how adding a plugin touches no engine file.

Validation is strict and loud: a manifest is part of the authority model (it
defines the lattice grants are narrowed in), so a typo must stop the broker
from loading the plugin rather than quietly produce a looser lattice. The
rules are listed in docs/manifest-schema.md; each has a failing test.

Two dimensions are built in and therefore reserved: `mode` (level
draft < direct, on every action with `modes`) and `budget` (per_minute,
per_day). A manifest cannot redeclare them.
"""

import re
import string
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import (BaseModel, ConfigDict, Field, PrivateAttr, ValidationError,
                      field_validator, model_validator)

from .params_schema import ParamsSchemaError, build_params_model, property_names

ID_RE = re.compile(r"^[a-z][a-z0-9]*$")
# Action and dimension names: MCP tools are "<plugin>_<action>", and plugin ids
# have no underscore, so the split back to (plugin, action) is unambiguous.
NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")

FORMS = ("list", "subtree", "pattern", "range", "flag", "level")
SET_FORMS = frozenset({"list", "subtree", "pattern"})      # stored in cap.selector
SCALAR_FORMS = frozenset({"range", "flag", "level"})       # stored in cap.constraints
SIDE_EFFECTS = ("read", "write", "destructive")
MODES = ("draft", "direct")                                 # low -> high
RESERVED_NAMES = frozenset({"mode", "budget", "target", "actions", "selector",
                            "constraints", "expires_at"})
# Action-set sugar accepted on input and expanded at creation (never stored).
ACTION_GLOBS = {"*": None, "read_*": "read", "write_*": "write",
                "destructive_*": "destructive"}


class ManifestError(ValueError):
    """The manifest is invalid; the message names the offending field."""


class _Model(BaseModel):
    # Unknown keys are errors: a misspelt key (say `enforcment`) must not
    # silently fall back to a default.
    model_config = ConfigDict(extra="forbid", frozen=True)


class Connection(_Model):
    # `none` is for in-process plugins holding no credential (the `echo`
    # test plugin); real targets use one of the other three.
    kind: Literal["sidecar_qr", "github_app", "google_oauth", "none"]
    shared: str | None = None            # shared credential slot, e.g. "google"
    enforcement: Literal["target", "proxy"] = "proxy"


class ConfigField(_Model):
    name: str
    type: Literal["string", "text", "integer", "boolean", "enum"]
    secret: bool = False
    # Belongs to the shared connection (`connection.shared`, e.g. the one
    # Google OAuth client behind gmail, gcal and gdrive), not to this plugin
    # alone: the console shows it once per shared slot, the broker keeps the
    # non-secret value identical on every plugin of that slot, and the
    # plugin runtime stores a shared secret in the connection's slot.
    shared: bool = False
    required: bool = False
    default: Any = None
    help: str = ""
    values: list[str] | None = None      # enum choices

    @model_validator(mode="after")
    def _check(self):
        if not NAME_RE.match(self.name):
            raise ValueError(f"config field name {self.name!r} must match {NAME_RE.pattern}")
        if (self.type == "enum") != bool(self.values):
            raise ValueError(f"config field {self.name}: enum needs values (and only enum has them)")
        if self.secret and self.default is not None:
            raise ValueError(f"config field {self.name}: secrets cannot have a default")
        if self.default is not None and not _config_default_ok(self):
            raise ValueError(f"config field {self.name}: default does not match type {self.type}")
        return self


def _config_default_ok(f: ConfigField) -> bool:
    d = f.default
    if f.type in ("string", "text"):
        return isinstance(d, str)
    if f.type == "integer":
        return isinstance(d, int) and not isinstance(d, bool)
    if f.type == "boolean":
        return isinstance(d, bool)
    return d in (f.values or [])


class Resource(_Model):
    display: str
    normalize: str | None = None         # adapter normalizer name (e.g. "jid")
    resolve: bool = False                # adapter can resolve names to ids
    hideable: bool = False               # owner may hide instances (hidden == 404)
    id_format: str = ""                  # human description of the canonical id


class Narrowing(_Model):
    dimension: str
    form: Literal["list", "subtree", "pattern", "range", "flag", "level"]
    applies_to: list[str]                # action names, or ["*"] for all
    enforcement: Literal["target", "proxy"] = "proxy"
    # Derived narrowings (e.g. GitHub `permissions`, Google `scopes`) are
    # computed from the allowed actions' target_permissions; they are not
    # stored in capabilities and cannot be set by a grant.
    derived_from: Literal["target_permissions"] | None = None
    values: list[str] | None = None      # ordered low -> high; `level` only
    resource: str | None = None          # resource kind the ids belong to
    doc: str = ""


class Constraint(_Model):
    name: str
    # Only scalar forms: a set-valued constraint would be an allow-list, which
    # is a narrowing. (Deny-shaped rules belong in denies, not the lattice.)
    form: Literal["range", "flag", "level"]
    applies_to: list[str]
    default: Any = None                  # console pre-fill; absent == unconstrained
    enforcement: Literal["target", "proxy"] = "proxy"
    values: list[str] | None = None      # `level` only
    doc: str = ""


class Action(_Model):
    name: str
    side_effect: Literal["read", "write", "destructive"]
    resource: str | None = None          # resource kind this action addresses
    selector_param: str | None = None    # the param holding that resource's id
    params: dict = Field(default_factory=lambda: {"type": "object", "properties": {}})
    modes: list[Literal["draft", "direct"]] | None = None
    schedulable: bool = False
    long_poll: bool = False
    returns: Literal["json", "binary"] = "json"
    target_permissions: dict[str, str] = Field(default_factory=dict)
    summary_template: str | None = None
    doc: str = ""

    _params_model: Any = PrivateAttr(default=None)

    @property
    def params_model(self) -> type[BaseModel]:
        """Pydantic model validating this action's params (strict, no extras)."""
        return self._params_model

    @property
    def effective_modes(self) -> tuple[str, ...]:
        """Modes this action supports. Reads are always direct; writes default
        to both so every side effect can be routed through a human."""
        if self.modes is not None:
            return tuple(self.modes)
        return ("direct",) if self.side_effect == "read" else ("direct", "draft")


class SkillExample(_Model):
    title: str
    action: str
    params: dict = Field(default_factory=dict)


class Skill(_Model):
    addressing: str = ""
    rules: list[str] = Field(default_factory=list)
    examples: list[SkillExample] = Field(default_factory=list)


class Manifest(_Model):
    id: str
    version: str
    display_name: str
    description: str = ""
    connection: Connection
    config_schema: list[ConfigField] = Field(default_factory=list)
    resources: dict[str, Resource] = Field(default_factory=dict)
    narrowings: list[Narrowing] = Field(default_factory=list)
    constraints: list[Constraint] = Field(default_factory=list)
    actions: list[Action]
    skill: Skill = Field(default_factory=Skill)

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not ID_RE.match(v):
            raise ValueError(f"plugin id {v!r} must match {ID_RE.pattern}")
        return v

    @field_validator("version")
    @classmethod
    def _version(cls, v: str) -> str:
        if not VERSION_RE.match(v):
            raise ValueError(f"version {v!r} must be MAJOR.MINOR.PATCH")
        return v

    @model_validator(mode="after")
    def _cross_checks(self):
        _validate(self)
        return self

    # ---- derived views ---------------------------------------------------

    def action(self, name: str) -> Action | None:
        return next((a for a in self.actions if a.name == name), None)

    @property
    def action_names(self) -> frozenset[str]:
        return frozenset(a.name for a in self.actions)

    def actions_by_effect(self, *effects: str) -> frozenset[str]:
        return frozenset(a.name for a in self.actions if a.side_effect in effects)

    def expand_actions(self, names) -> frozenset[str]:
        """Expand input sugar (`*`, `read_*`, `write_*`, `destructive_*`) into
        explicit action names. Unknown names and any other glob raise, so a
        typo never becomes an accidental wildcard."""
        out: set[str] = set()
        for n in names:
            if n in ACTION_GLOBS:
                effect = ACTION_GLOBS[n]
                out |= self.action_names if effect is None else self.actions_by_effect(effect)
            elif n in self.action_names:
                out.add(n)
            else:
                raise ManifestError(f"{self.id}: unknown action {n!r}")
        return frozenset(out)


def _validate(m: Manifest) -> None:
    """Cross-field rules pydantic field types cannot express."""
    names = [a.name for a in m.actions]
    if not names:
        raise ValueError("a manifest needs at least one action")
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise ValueError(f"duplicate action names: {sorted(dupes)}")
    for n in names:
        if not NAME_RE.match(n):
            raise ValueError(f"action name {n!r} must match {NAME_RE.pattern}")
        if n in ACTION_GLOBS:
            raise ValueError(f"action name {n!r} collides with glob sugar")
    cfg = [f.name for f in m.config_schema]
    if len(cfg) != len(set(cfg)):
        raise ValueError("duplicate config_schema names")
    shared = [f.name for f in m.config_schema if f.shared]
    if shared and not m.connection.shared:
        # A shared field needs a slot to live in; without one it would
        # silently become per-plugin, which is not what the author declared.
        raise ValueError(f"config fields {shared} are shared but connection.shared is not set")
    for kind in m.resources:
        if not NAME_RE.match(kind):
            raise ValueError(f"resource kind {kind!r} must match {NAME_RE.pattern}")

    dims = [n.dimension for n in m.narrowings] + [c.name for c in m.constraints]
    dupes = {d for d in dims if dims.count(d) > 1}
    if dupes:
        raise ValueError(f"duplicate narrowing/constraint names: {sorted(dupes)}")
    for d in dims:
        if not NAME_RE.match(d):
            raise ValueError(f"dimension name {d!r} must match {NAME_RE.pattern}")
        if d in RESERVED_NAMES:
            raise ValueError(f"{d!r} is built in and cannot be declared")

    action_set = set(names)
    for n in m.narrowings:
        _check_applies_to(n.dimension, n.applies_to, action_set)
        _check_values(n.dimension, n.form, n.values)
        if n.resource is not None and n.resource not in m.resources:
            raise ValueError(f"narrowing {n.dimension}: unknown resource kind {n.resource!r}")
        if n.form == "subtree" and n.resource is None and not n.derived_from:
            raise ValueError(f"narrowing {n.dimension}: subtree needs a resource kind "
                             "(ancestry is walked per kind)")
    for c in m.constraints:
        _check_applies_to(c.name, c.applies_to, action_set)
        _check_values(c.name, c.form, c.values)
        if c.default is not None and not _scalar_ok(c.form, c.default, c.values):
            raise ValueError(f"constraint {c.name}: default does not match form {c.form}")

    for a in m.actions:
        _validate_action(m, a)
    for ex in m.skill.examples:
        act = m.action(ex.action)
        if act is None:
            raise ValueError(f"skill example {ex.title!r}: unknown action {ex.action!r}")
        try:
            act.params_model.model_validate(ex.params)
        except ValidationError as exc:
            raise ValueError(f"skill example {ex.title!r}: params invalid: {exc}") from exc


def _validate_action(m: Manifest, a: Action) -> None:
    try:
        model = build_params_model(a.params, f"{m.id}_{a.name}_params")
    except ParamsSchemaError as exc:
        raise ValueError(f"action {a.name}: {exc}") from exc
    # PrivateAttr on a frozen model: set once, here, at load.
    a.__pydantic_private__["_params_model"] = model
    params = property_names(a.params)
    if a.resource is not None and a.resource not in m.resources:
        raise ValueError(f"action {a.name}: unknown resource kind {a.resource!r}")
    if a.selector_param is not None:
        if a.selector_param not in params:
            raise ValueError(f"action {a.name}: selector_param {a.selector_param!r} "
                             "is not a declared param")
        if a.resource is None:
            raise ValueError(f"action {a.name}: selector_param needs a resource kind")
    if a.modes is not None:
        if not a.modes or len(set(a.modes)) != len(a.modes):
            raise ValueError(f"action {a.name}: modes must be a non-empty unique list")
        if a.side_effect == "read" and a.modes != ["direct"]:
            raise ValueError(f"action {a.name}: reads are always direct")
    if a.schedulable and a.side_effect == "read":
        raise ValueError(f"action {a.name}: only writes can be schedulable")
    if a.long_poll and a.side_effect != "read":
        raise ValueError(f"action {a.name}: only reads can long-poll")
    if a.summary_template is not None:
        _check_template(a.name, a.summary_template, params)


def _check_applies_to(owner: str, applies_to: list[str], actions: set[str]) -> None:
    if not applies_to:
        raise ValueError(f"{owner}: applies_to must name at least one action (or '*')")
    if applies_to == ["*"]:
        return
    unknown = [x for x in applies_to if x not in actions]
    if unknown:
        raise ValueError(f"{owner}: applies_to names unknown actions {unknown}")


def _check_values(owner: str, form: str, values: list[str] | None) -> None:
    if form == "level":
        if not values or len(values) < 2 or len(set(values)) != len(values):
            raise ValueError(f"{owner}: a level needs >= 2 unique ordered values")
    elif values is not None:
        raise ValueError(f"{owner}: values are only for level forms")


def _scalar_ok(form: str, value: Any, values: list[str] | None) -> bool:
    if form == "range":
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0
    if form == "flag":
        return isinstance(value, bool)
    return value in (values or [])


def _check_template(action: str, template: str, params: set[str]) -> None:
    try:
        fields = [f for _, f, _, _ in string.Formatter().parse(template) if f is not None]
    except ValueError as exc:
        raise ValueError(f"action {action}: bad summary_template: {exc}") from exc
    allowed = params | {f"{p}_label" for p in params}
    for f in fields:
        # Plain names only: "{to.__class__}" would be attribute access.
        if f not in allowed:
            raise ValueError(f"action {action}: summary_template placeholder {{{f}}} "
                             "is not a param or <param>_label")


# ---- loading ---------------------------------------------------------------

def load_manifest_text(text: str) -> Manifest:
    """Parse and validate manifest YAML. Raises ManifestError on any problem."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ManifestError(f"manifest is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ManifestError("manifest must be a mapping")
    try:
        return Manifest.model_validate(data)
    except ValidationError as exc:
        raise ManifestError(str(exc)) from exc


def load_manifest(path: str | Path) -> Manifest:
    """Load and validate a manifest file (targets/<id>/manifest.yaml)."""
    return load_manifest_text(Path(path).read_text(encoding="utf-8"))
