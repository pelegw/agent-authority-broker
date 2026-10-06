"""The plugin package descriptor, `aab-plugin.yaml`: what an external plugin asks for.

A plugin repository describes itself in this one small file; the installer
renders the compose overlay from it through a fixed template (overlay.py), so
a plugin repository can never supply compose YAML of its own. Validation is
strict and loud for the same reason the broker's manifest validation is: the
descriptor decides which volumes, environment and network a container gets,
and a value that is merely "odd" must stop the install, not produce an odd
overlay.

Schema 1 (docs/plugin-packaging.md):

    schema: 1
    service: finance                      # compose service plugin-finance, network net_finance
    plugins: [finance]                    # the manifest ids this service hosts
    manifests: [aab_plugin_finance/manifest.yaml]   # one per plugin id, same order
    runtime: "0.3"                        # the gateway line it was built against (informational)
    build: {dockerfile: Dockerfile}       # the context is always the repository root
    volumes: {finance_data: /data}        # named volumes only, names start with "<service>_"
    environment: {FINANCE_DB: /data/finance.db}   # literal values only
    env_passthrough: [TZ]                 # host .env keys it may read: TZ, LOG_LEVEL, LOG_FORMAT

Why each rule exists (each has a rejecting test):
  * `service` is one lowercase word, and the names the stack already uses
    are reserved: `net_<service>` must never be `net_installer` (the
    installer is root on the host) or another plugin's network, and
    `plugin-<service>` must never replace an in-tree service.
  * Volume names start with `<service>_` (service names have no
    underscore, so the prefix names exactly one service): a plugin can never
    mount another service's volume, the WhatsApp session, or the broker's
    database. `<service>_secrets` is the installer's, at /secrets.
  * No string the template interpolates may contain `$`: compose would
    expand `${PLUGIN_TOKEN_GITHUB}` in a "literal" value from the .env that
    holds every service's secrets.
  * Paths are relative, POSIX and free of `..`, so a manifest or Dockerfile
    path can never leave the cloned repository.
"""

from __future__ import annotations

import re
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

DESCRIPTOR_FILE = "aab-plugin.yaml"
# A descriptor is a dozen lines; anything near this is not one.
MAX_DESCRIPTOR_BYTES = 64 * 1024

SERVICE_RE = re.compile(r"^[a-z][a-z0-9]{1,31}$")
PLUGIN_ID_RE = re.compile(r"^[a-z][a-z0-9]*$")          # the broker's manifest ID_RE
VOLUME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
RUNTIME_RE = re.compile(r"^\d+\.\d+(?:\.\d+)?$")
# A relative POSIX path inside the repository: no leading "/", no "\", no ":".
REL_PATH_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-/]{0,199}$")
# An absolute path in the container: what a named volume is mounted at.
MOUNT_RE = re.compile(r"^/[A-Za-z0-9_.\-/]{0,199}$")

# Host .env keys a plugin may read (the installer renders `${KEY:-default}`).
PASSTHROUGH_DEFAULTS = {"TZ": "UTC", "LOG_LEVEL": "INFO", "LOG_FORMAT": "text"}
# Service names the stack already uses for compose services, networks or
# volume prefixes (broker_data, wa_data, wa_session, caddy_data, the in-tree
# plugin services and their <service>_secrets), plus the installer's own.
RESERVED_SERVICES = frozenset({
    "broker", "edge", "caddy", "installer", "sidecar", "internal", "default",
    "whatsapp", "wa", "github", "google", "plugin", "plugins",
})
# Set by the installer; a descriptor can never set or shadow them.
RESERVED_ENV_PREFIXES = ("PLUGIN_",)
MAX_PLUGINS = 16
MAX_VOLUMES = 8
MAX_ENV = 32
MAX_ENV_VALUE = 1024


class DescriptorError(ValueError):
    """The descriptor is invalid; the message names the offending field."""


def _rel_path(value: str, what: str) -> str:
    if not REL_PATH_RE.match(value) or any(p in ("", ".", "..") for p in value.split("/")):
        raise ValueError(f"{what} {value!r} must be a relative path inside the repository "
                         "(letters, digits, _ . - /; no '..', no empty segment)")
    return value


class _Model(BaseModel):
    # Unknown keys are errors: a misspelt `enviroment` must not silently drop
    # what the author meant (or smuggle in what the template never renders).
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Build(_Model):
    dockerfile: str = "Dockerfile"

    @field_validator("dockerfile")
    @classmethod
    def _dockerfile(cls, v: str) -> str:
        return _rel_path(v, "build.dockerfile")


class Descriptor(_Model):
    schema_: Literal[1] = Field(alias="schema")
    service: str
    plugins: list[str] = Field(min_length=1, max_length=MAX_PLUGINS)
    manifests: list[str] = Field(min_length=1, max_length=MAX_PLUGINS)
    runtime: str
    build: Build = Field(default_factory=Build)
    volumes: dict[str, str] = Field(default_factory=dict, max_length=MAX_VOLUMES)
    environment: dict[str, str] = Field(default_factory=dict, max_length=MAX_ENV)
    env_passthrough: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, populate_by_name=True)

    @field_validator("service")
    @classmethod
    def _service(cls, v: str) -> str:
        if not SERVICE_RE.match(v):
            raise ValueError(f"service {v!r} must match {SERVICE_RE.pattern}")
        if v in RESERVED_SERVICES:
            raise ValueError(f"service {v!r} is a name the stack already uses")
        return v

    @field_validator("plugins")
    @classmethod
    def _plugins(cls, v: list[str]) -> list[str]:
        for pid in v:
            if not PLUGIN_ID_RE.match(pid):
                raise ValueError(f"plugin id {pid!r} must match {PLUGIN_ID_RE.pattern}")
        if len(set(v)) != len(v):
            raise ValueError("plugins must be unique")
        return v

    @field_validator("manifests")
    @classmethod
    def _manifests(cls, v: list[str]) -> list[str]:
        for path in v:
            _rel_path(path, "manifest path")
            if not path.endswith((".yaml", ".yml")):
                raise ValueError(f"manifest path {path!r} must be a .yaml file")
        if len(set(v)) != len(v):
            raise ValueError("manifests must be unique")
        return v

    @field_validator("runtime")
    @classmethod
    def _runtime(cls, v: str) -> str:
        if not RUNTIME_RE.match(v):
            raise ValueError(f"runtime {v!r} must look like 0.3 or 0.3.0")
        return v

    @field_validator("env_passthrough")
    @classmethod
    def _passthrough(cls, v: list[str]) -> list[str]:
        for name in v:
            if name not in PASSTHROUGH_DEFAULTS:
                raise ValueError(f"env_passthrough {name!r} is not allowed; choose from "
                                 f"{sorted(PASSTHROUGH_DEFAULTS)}")
        if len(set(v)) != len(v):
            raise ValueError("env_passthrough must be unique")
        return v

    @model_validator(mode="after")
    def _cross(self):
        if len(self.manifests) != len(self.plugins):
            raise ValueError("manifests must list one file per plugin id, in the same order")
        own = f"{self.service}_"
        targets = []
        for name, target in self.volumes.items():
            if not VOLUME_RE.match(name):
                raise ValueError(f"volume name {name!r} must match {VOLUME_RE.pattern}")
            if not name.startswith(own) or name == own:
                raise ValueError(f"volume name {name!r} must start with {own!r} "
                                 "(a plugin names only its own volumes)")
            if name == f"{self.service}_secrets":
                raise ValueError(f"volume {name!r} is the installer's (mounted at /secrets)")
            if not MOUNT_RE.match(target) or any(p in (".", "..") for p in target.split("/")):
                raise ValueError(f"volume {name}: mount path {target!r} must be an absolute "
                                 "path (letters, digits, _ . - /; no '..')")
            norm = target.rstrip("/") or "/"
            if norm == "/" or norm == "/secrets" or norm.startswith("/secrets/"):
                raise ValueError(f"volume {name}: {target!r} is not a mount path a plugin may use")
            targets.append(norm)
        if len(set(targets)) != len(targets):
            raise ValueError("two volumes are mounted at the same path")
        for key, value in self.environment.items():
            if not ENV_NAME_RE.match(key):
                raise ValueError(f"environment name {key!r} must match {ENV_NAME_RE.pattern}")
            if key.startswith(RESERVED_ENV_PREFIXES):
                raise ValueError(f"environment name {key!r} is set by the installer")
            if key in PASSTHROUGH_DEFAULTS:
                raise ValueError(f"environment name {key!r} comes from env_passthrough")
            _literal(key, value)
        return self


def _literal(key: str, value: str) -> None:
    if len(value) > MAX_ENV_VALUE:
        raise ValueError(f"environment {key}: value longer than {MAX_ENV_VALUE} characters")
    if "$" in value:
        # Compose would interpolate it from the .env holding every secret.
        raise ValueError(f"environment {key}: '$' is not allowed in a literal value")
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        raise ValueError(f"environment {key}: control characters are not allowed")


def parse(text: str | bytes) -> Descriptor:
    """Parse and validate descriptor YAML. Raises DescriptorError."""
    if isinstance(text, bytes):
        if len(text) > MAX_DESCRIPTOR_BYTES:
            raise DescriptorError(f"{DESCRIPTOR_FILE} is larger than {MAX_DESCRIPTOR_BYTES} bytes")
        try:
            text = text.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DescriptorError(f"{DESCRIPTOR_FILE} is not UTF-8") from exc
    if len(text) > MAX_DESCRIPTOR_BYTES:
        raise DescriptorError(f"{DESCRIPTOR_FILE} is larger than {MAX_DESCRIPTOR_BYTES} bytes")
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise DescriptorError(f"{DESCRIPTOR_FILE} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise DescriptorError(f"{DESCRIPTOR_FILE} must be a mapping")
    try:
        return Descriptor.model_validate(data)
    except ValidationError as exc:
        raise DescriptorError(_describe(exc)) from exc


def _describe(exc: ValidationError) -> str:
    """One line per problem, field path first: shown to the owner as is."""
    out = []
    for err in exc.errors(include_url=False, include_input=False):
        where = ".".join(str(p) for p in err.get("loc", ())) or DESCRIPTOR_FILE
        out.append(f"{where}: {err.get('msg', 'invalid')}")
    return "; ".join(out)


def view(d: Descriptor) -> dict:
    """The descriptor as plain JSON data, with its YAML key names."""
    return {"schema": 1, "service": d.service, "plugins": list(d.plugins),
            "manifests": list(d.manifests), "runtime": d.runtime,
            "build": {"dockerfile": d.build.dockerfile}, "volumes": dict(d.volumes),
            "environment": dict(d.environment), "env_passthrough": list(d.env_passthrough)}
