"""Turn an action's `params` JSON-schema subset into a pydantic model.

Manifests describe action parameters in a deliberately small JSON-schema
subset so the same text can drive REST validation, MCP tool schemas and the
console, without the broker shipping a full JSON-schema engine. Anything
outside the subset raises `ParamsSchemaError` at load time: a keyword we
silently ignored (say `pattern` or `additionalProperties`) would be a
validation rule the author believes is enforced but is not.

Supported keywords:
  type (object | string | integer | boolean | array), properties, required,
  enum, default, minimum, maximum, minLength, maxLength, items, description.

Generated models are strict (no "5" -> 5 coercion) and forbid unknown
parameters, so an agent cannot smuggle a field an adapter might read.
"""

from typing import Annotated, Any, Literal

from pydantic import (BaseModel, ConfigDict, Field, TypeAdapter, ValidationError,
                      create_model)

_TYPES = {"object", "string", "integer", "boolean", "array"}
_COMMON = {"type", "description", "default", "enum"}
# Keywords allowed per type, on top of _COMMON.
_BY_TYPE = {
    "object": {"properties", "required"},
    "string": {"minLength", "maxLength"},
    "integer": {"minimum", "maximum"},
    "boolean": set(),
    "array": {"items"},
}
_PY = {"string": str, "integer": int, "boolean": bool}


class ParamsSchemaError(ValueError):
    """The params schema uses something outside the supported subset."""


class _Strict(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


def build_params_model(schema: dict, model_name: str) -> type[BaseModel]:
    """Build the pydantic model for a top-level params schema (must be an object)."""
    if not isinstance(schema, dict):
        raise ParamsSchemaError(f"{model_name}: params must be a mapping")
    if schema.get("type", "object") != "object":
        raise ParamsSchemaError(f"{model_name}: top-level params must be type: object")
    schema = {"type": "object", **schema}
    return _object_model(schema, model_name, path=model_name)


def property_names(schema: dict) -> set[str]:
    """Top-level parameter names declared by a (validated) params schema."""
    return set((schema or {}).get("properties", {}) or {})


def _object_model(schema: dict, name: str, path: str) -> type[BaseModel]:
    _check_keys(schema, "object", path)
    if "default" in schema or "enum" in schema:
        raise ParamsSchemaError(f"{path}: default/enum are not supported on objects")
    props = schema.get("properties", {}) or {}
    required = schema.get("required", []) or []
    if not isinstance(props, dict):
        raise ParamsSchemaError(f"{path}: properties must be a mapping")
    if not isinstance(required, list) or not all(isinstance(r, str) for r in required):
        raise ParamsSchemaError(f"{path}: required must be a list of names")
    missing = set(required) - set(props)
    if missing:
        raise ParamsSchemaError(f"{path}: required names not in properties: {sorted(missing)}")
    fields: dict[str, Any] = {}
    for prop, sub in props.items():
        if not isinstance(prop, str) or not prop.isidentifier() or prop.startswith("_"):
            raise ParamsSchemaError(f"{path}: invalid property name {prop!r}")
        if not isinstance(sub, dict):
            raise ParamsSchemaError(f"{path}.{prop}: schema must be a mapping")
        py_type, field_kwargs = _field(sub, f"{path}.{prop}", f"{name}_{prop}")
        is_required = prop in required
        if is_required and "default" in sub:
            raise ParamsSchemaError(f"{path}.{prop}: a required param cannot have a default")
        if "default" in sub:
            # pydantic does not validate defaults unless asked; do it once here
            # so a manifest cannot ship a default its own rules would reject.
            _check_default(py_type, field_kwargs, sub["default"], f"{path}.{prop}")
        if is_required:
            fields[prop] = (py_type, Field(..., **field_kwargs))
        elif "default" in sub:
            fields[prop] = (py_type, Field(sub["default"], **field_kwargs))
        else:
            fields[prop] = (py_type | None, Field(None, **field_kwargs))
    try:
        return create_model(name, __base__=_Strict, **fields)
    except Exception as exc:  # pydantic raises several types for bad field specs
        raise ParamsSchemaError(f"{path}: {exc}") from exc


def _field(sub: dict, path: str, name: str) -> tuple[Any, dict]:
    t = sub.get("type")
    if t not in _TYPES:
        raise ParamsSchemaError(f"{path}: type must be one of {sorted(_TYPES)}, got {t!r}")
    _check_keys(sub, t, path)
    kwargs: dict[str, Any] = {}
    if "description" in sub:
        if not isinstance(sub["description"], str):
            raise ParamsSchemaError(f"{path}: description must be a string")
        kwargs["description"] = sub["description"]
    if t == "object":
        return _object_model(sub, name, path), kwargs
    if t == "array":
        items = sub.get("items")
        if not isinstance(items, dict):
            raise ParamsSchemaError(f"{path}: arrays need an items schema")
        item_type, _ = _field(items, f"{path}[]", f"{name}_item")
        return list[item_type], kwargs
    py: Any = _PY[t]
    if "enum" in sub:
        values = sub["enum"]
        if t == "boolean" or not isinstance(values, list) or not values:
            raise ParamsSchemaError(f"{path}: enum must be a non-empty list on string/integer")
        if not all(isinstance(v, py) and not isinstance(v, bool) for v in values):
            raise ParamsSchemaError(f"{path}: enum values must all be {t}")
        py = Literal[tuple(values)]
    for key, arg in (("minimum", "ge"), ("maximum", "le")):
        if key in sub:
            if not isinstance(sub[key], int) or isinstance(sub[key], bool):
                raise ParamsSchemaError(f"{path}: {key} must be an integer")
            kwargs[arg] = sub[key]
    for key, arg in (("minLength", "min_length"), ("maxLength", "max_length")):
        if key in sub:
            if not isinstance(sub[key], int) or isinstance(sub[key], bool) or sub[key] < 0:
                raise ParamsSchemaError(f"{path}: {key} must be a non-negative integer")
            kwargs[arg] = sub[key]
    return py, kwargs


def _check_keys(sub: dict, t: str, path: str) -> None:
    unknown = set(sub) - _COMMON - _BY_TYPE[t]
    if unknown:
        raise ParamsSchemaError(
            f"{path}: unsupported keyword(s) for type {t}: {sorted(unknown)} "
            "(the params subset is documented in docs/manifest-schema.md)")


def _check_default(py_type: Any, field_kwargs: dict, value: Any, path: str) -> None:
    try:
        TypeAdapter(Annotated[py_type, Field(**field_kwargs)],
                    config=ConfigDict(strict=True)).validate_python(value)
    except ValidationError as exc:
        raise ParamsSchemaError(f"{path}: default {value!r} is invalid: {exc}") from exc
