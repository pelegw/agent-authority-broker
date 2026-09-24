"""Manifest loading: the shipped manifests validate, every validation rule
has a failing case, and params outside the JSON-schema subset fail loudly."""

import copy

import pytest
import yaml
from pydantic import ValidationError

from broker.plugins.manifest import ManifestError, load_manifest_text

from .helpers import ECHO, GITHUB, WHATSAPP

BASE = {
    "id": "demo",
    "version": "1.0.0",
    "display_name": "Demo",
    "connection": {"kind": "none"},
    "resources": {"room": {"display": "Room", "hideable": True}},
    "narrowings": [{"dimension": "room", "form": "list", "resource": "room",
                    "applies_to": ["send"]}],
    "constraints": [{"name": "window", "form": "range", "applies_to": ["read"], "default": 7}],
    "actions": [
        {"name": "read", "side_effect": "read", "resource": "room", "selector_param": "room",
         "params": {"type": "object", "properties": {"room": {"type": "string"}}}},
        {"name": "send", "side_effect": "write", "resource": "room", "selector_param": "room",
         "summary_template": "To {room_label}: {text}",
         "params": {"type": "object",
                    "properties": {"room": {"type": "string"}, "text": {"type": "string"}},
                    "required": ["room", "text"]}},
    ],
}


def load(data):
    return load_manifest_text(yaml.safe_dump(data))


def mutated(fn):
    data = copy.deepcopy(BASE)
    fn(data)
    return data


def test_base_fixture_is_valid():
    m = load(BASE)
    assert m.id == "demo" and m.action_names == {"read", "send"}


def test_shipped_manifests_load():
    assert ECHO.id == "echo" and WHATSAPP.id == "whatsapp" and GITHUB.id == "github"
    send = WHATSAPP.action("send_message")
    assert send.side_effect == "write" and send.schedulable
    assert send.summary_template == "Send to {to_label}: {text}"
    assert {a.name for a in WHATSAPP.actions} == {
        "list_chats", "get_chat", "read_messages", "search_messages", "check_new_messages",
        "search_contacts", "get_media", "send_message"}
    assert WHATSAPP.action("get_media").returns == "binary"
    assert WHATSAPP.action("check_new_messages").long_poll
    assert GITHUB.actions_by_effect("destructive") == {"merge_pr", "delete_branch"}
    assert GITHUB.action("get_file").target_permissions == {"contents": "read"}


def test_echo_exercises_all_six_forms_and_side_effects():
    forms = {n.form for n in ECHO.narrowings} | {c.form for c in ECHO.constraints}
    assert forms == {"list", "subtree", "pattern", "range", "flag", "level"}
    assert {a.side_effect for a in ECHO.actions} == {"read", "write", "destructive"}


def test_params_model_is_strict_and_forbids_extras():
    model = WHATSAPP.action("send_message").params_model
    assert model(to="x", text="hi").text == "hi"
    with pytest.raises(ValidationError):
        model(to="x", text="hi", bcc="evil")          # unknown param
    with pytest.raises(ValidationError):
        model(to="x")                                  # missing required
    with pytest.raises(ValidationError):
        model(to="x", text="")                         # minLength
    limit_model = WHATSAPP.action("read_messages").params_model
    assert limit_model(chat="c").limit == 30           # default applied
    with pytest.raises(ValidationError):
        limit_model(chat="c", limit="5")               # no str -> int coercion
    with pytest.raises(ValidationError):
        limit_model(chat="c", limit=500)               # maximum
    enum_model = ECHO.action("post_item").params_model
    assert enum_model(room="r", text="t", tags=["a"]).priority == "normal"
    with pytest.raises(ValidationError):
        enum_model(room="r", text="t", priority="urgent")


def test_default_modes():
    assert WHATSAPP.action("read_messages").effective_modes == ("direct",)
    assert GITHUB.action("create_issue").effective_modes == ("direct", "draft")


def test_expand_actions_sugar():
    assert ECHO.expand_actions(["*"]) == ECHO.action_names
    assert ECHO.expand_actions(["read_*"]) == {"list_items", "get_item", "get_blob", "watch"}
    assert ECHO.expand_actions(["write_*", "destructive_*"]) == {"post_item", "delete_item",
                                                                 "touch_item"}
    with pytest.raises(ManifestError):
        ECHO.expand_actions(["post_*"])                # only the documented globs
    with pytest.raises(ManifestError):
        ECHO.expand_actions(["nope"])


# Each case breaks exactly one rule of docs/manifest-schema.md.
INVALID = {
    "bad id (uppercase)": lambda d: d.update(id="Demo"),
    "bad id (underscore)": lambda d: d.update(id="my_plugin"),
    "bad version": lambda d: d.update(version="1.0"),
    "unknown top-level key": lambda d: d.update(extra=1),
    "unknown connection kind": lambda d: d["connection"].update(kind="oauth1"),
    "misspelt key": lambda d: d["connection"].update(enforcment="target"),
    "no actions": lambda d: d.update(actions=[]),
    "duplicate action": lambda d: d["actions"].append(copy.deepcopy(d["actions"][0])),
    "bad action name": lambda d: d["actions"][0].update(name="Read-It"),
    "action named like a glob": lambda d: d["actions"][0].update(name="read_*"),
    "bad side effect": lambda d: d["actions"][0].update(side_effect="delete"),
    "applies_to unknown action": lambda d: d["narrowings"][0].update(applies_to=["nope"]),
    "applies_to empty": lambda d: d["narrowings"][0].update(applies_to=[]),
    "constraint applies_to unknown": lambda d: d["constraints"][0].update(applies_to=["x"]),
    "unknown form": lambda d: d["narrowings"][0].update(form="regex"),
    "set-form constraint": lambda d: d["constraints"][0].update(form="list"),
    "level without values": lambda d: d["narrowings"][0].update(form="level"),
    "level with one value": lambda d: d["narrowings"][0].update(form="level", values=["a"]),
    "values on a list": lambda d: d["narrowings"][0].update(values=["a", "b"]),
    "subtree without resource": lambda d: d["narrowings"][0].update(form="subtree", resource=None),
    "narrowing unknown resource": lambda d: d["narrowings"][0].update(resource="ghost"),
    "duplicate dimension": lambda d: d["constraints"].append(
        {"name": "room", "form": "flag", "applies_to": ["read"]}),
    "reserved dimension mode": lambda d: d["narrowings"].append(
        {"dimension": "mode", "form": "level", "values": ["a", "b"], "applies_to": ["*"]}),
    "reserved dimension budget": lambda d: d["constraints"].append(
        {"name": "budget", "form": "range", "applies_to": ["*"]}),
    "constraint default wrong type": lambda d: d["constraints"][0].update(default="7"),
    "selector_param not a param": lambda d: d["actions"][0].update(selector_param="chat"),
    "selector_param without resource": lambda d: d["actions"][0].update(resource=None),
    "action unknown resource": lambda d: d["actions"][0].update(resource="ghost"),
    "template unknown placeholder": lambda d: d["actions"][1].update(
        summary_template="To {room_label}: {body}"),
    "template attribute access": lambda d: d["actions"][1].update(
        summary_template="{text.__class__}"),
    "template label of unknown param": lambda d: d["actions"][1].update(
        summary_template="{chat_label}"),
    "read with draft mode": lambda d: d["actions"][0].update(modes=["direct", "draft"]),
    "empty modes": lambda d: d["actions"][1].update(modes=[]),
    "schedulable read": lambda d: d["actions"][0].update(schedulable=True),
    "long_poll write": lambda d: d["actions"][1].update(long_poll=True),
    "bad returns": lambda d: d["actions"][0].update(returns="xml"),
    "config enum without values": lambda d: d.update(config_schema=[{"name": "c", "type": "enum"}]),
    "config secret with default": lambda d: d.update(
        config_schema=[{"name": "c", "type": "string", "secret": True, "default": "x"}]),
    "config default wrong type": lambda d: d.update(
        config_schema=[{"name": "c", "type": "integer", "default": "3"}]),
    "duplicate config": lambda d: d.update(config_schema=[
        {"name": "c", "type": "string"}, {"name": "c", "type": "string"}]),
    "skill example unknown action": lambda d: d.update(
        skill={"examples": [{"title": "t", "action": "nope"}]}),
    "skill example bad params": lambda d: d.update(
        skill={"examples": [{"title": "t", "action": "send", "params": {"room": "r"}}]}),
}


@pytest.mark.parametrize("case", sorted(INVALID))
def test_invalid_manifest_is_rejected(case):
    with pytest.raises(ManifestError):
        load(mutated(INVALID[case]))


# Params outside the supported JSON-schema subset must fail loudly.
BAD_PARAMS = {
    "pattern keyword": {"room": {"type": "string", "pattern": "^a"}},
    "additionalProperties": None,
    "format keyword": {"room": {"type": "string", "format": "email"}},
    "number type": {"room": {"type": "number"}},
    "oneOf": {"room": {"oneOf": [{"type": "string"}]}},
    "no type": {"room": {"minLength": 1}},
    "minimum on string": {"room": {"type": "string", "minimum": 1}},
    "minLength on integer": {"room": {"type": "integer", "minLength": 1}},
    "array without items": {"room": {"type": "array"}},
    "enum on boolean": {"room": {"type": "boolean", "enum": [True]}},
    "enum type mismatch": {"room": {"type": "string", "enum": [1, 2]}},
    "default violates rule": {"room": {"type": "integer", "maximum": 3, "default": 9}},
    "default wrong type": {"room": {"type": "string", "default": 5}},
    "underscore name": {"_room": {"type": "string"}},
}


@pytest.mark.parametrize("case", sorted(BAD_PARAMS))
def test_params_outside_subset_fail_loudly(case):
    def apply(d):
        action = d["actions"][0]
        action["selector_param"] = None
        action["resource"] = None
        if case == "additionalProperties":
            action["params"] = {"type": "object", "properties": {},
                                "additionalProperties": False}
        else:
            action["params"] = {"type": "object", "properties": BAD_PARAMS[case]}
    with pytest.raises(ManifestError, match="unsupported|type|must|invalid|items|name"):
        load(mutated(apply))


def test_required_with_default_and_required_unknown_fail():
    def req_default(d):
        d["actions"][0]["params"] = {"type": "object", "required": ["room"],
                                     "properties": {"room": {"type": "string", "default": "x"}}}
    def req_unknown(d):
        d["actions"][0]["params"] = {"type": "object", "required": ["ghost"],
                                     "properties": {"room": {"type": "string"}}}
    def top_not_object(d):
        d["actions"][0]["params"] = {"type": "string"}
    for fn in (req_default, req_unknown, top_not_object):
        with pytest.raises(ManifestError):
            load(mutated(fn))


def test_nested_object_params_supported():
    def nested(d):
        d["actions"][0].update(selector_param=None, params={
            "type": "object",
            "properties": {"filter": {"type": "object",
                                      "properties": {"since": {"type": "integer", "minimum": 0}},
                                      "required": ["since"]}}})
    m = load(mutated(nested))
    model = m.action("read").params_model
    assert model(filter={"since": 3}).filter.since == 3
    with pytest.raises(ValidationError):
        model(filter={"since": 3, "extra": 1})


def test_not_yaml_or_not_mapping():
    with pytest.raises(ManifestError):
        load_manifest_text("id: [unclosed")
    with pytest.raises(ManifestError):
        load_manifest_text("- just\n- a list\n")
