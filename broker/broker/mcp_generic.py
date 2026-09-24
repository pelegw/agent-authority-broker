"""The generic MCP tools: the key's own access, permissions and queued actions.

These are the same operations as the agent REST routes (`/v1/me`,
`/v1/permissions`, `/v1/delegations`, `/v1/actions`, `/v1/targets`), calling
the same services/agent.py and services/delegation.py functions, so a key can
do exactly the same over either surface. Arguments are validated by small
pydantic models (extra keys refused; `delegate` reuses the REST body model),
and each model's JSON schema is the tool's `inputSchema`, so the advertised
schema and the check can never disagree.

A tool may be `available` only to some keys: `delegate` is not listed for a
key already at the delegation depth limit (calling it anyway gets the same
400 `depth_exceeded` as REST, because the service checks, not the list).

Deliberately absent: any approve/reject tool (approval is a human act that
lives only on the admin plane), and anything that lists hidden resources.
The key-specific skill doc is the MCP resource `broker://skill`
(mcp_server.py).

This module must not import services/admin.py or identity/ (a test walks
the import graph from mcp_server.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from mcp import types
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .auth import max_delegation_depth
from .errors import PolicyError
from .routers.delegations import DelegateBody
from .routers.permissions import PermissionBody
from .services import agent, delegation


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _NoArgs(_Args):
    pass


class _GrantId(_Args):
    grant_id: str = Field(min_length=1, max_length=100)


class _ActionId(_Args):
    action_id: str = Field(min_length=1, max_length=100)


class _Page(_Args):
    limit: int = Field(default=50, ge=1, le=200)
    cursor: int | None = None


class _ActionPage(_Page):
    status: str | None = Field(default=None, max_length=20)


class _KeyId(_Args):
    key_id: int = Field(ge=1)


class _Resolve(_Args):
    target: str = Field(min_length=1, max_length=64)
    kind: str = Field(min_length=1, max_length=64)
    query: str = Field(default="", max_length=200)
    limit: int = Field(default=20, ge=1, le=50)


def _always(auth) -> bool:
    return True


def _can_delegate(auth) -> bool:
    return auth.depth < max_delegation_depth()


@dataclass(frozen=True)
class Generic:
    name: str
    description: str
    args: type[BaseModel]
    run: Callable[[Any, BaseModel], Any]
    read_only: bool = True
    # Whether the tool is LISTED for this key. Never a permission check:
    # the service behind the tool enforces everything on every call.
    available: Callable[[Any], bool] = _always

    def tool(self) -> types.Tool:
        return types.Tool(
            name=self.name, description=self.description,
            inputSchema=_schema(self.args),
            annotations=types.ToolAnnotations(readOnlyHint=self.read_only,
                                              destructiveHint=False))


def _schema(model: type[BaseModel]) -> dict:
    """The model's JSON schema without pydantic's `title` noise (agents pay
    tokens for every byte of the tool list)."""
    def strip(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: strip(v) for k, v in node.items() if k != "title"}
        if isinstance(node, list):
            return [strip(v) for v in node]
        return node
    out = strip(model.model_json_schema())
    out.setdefault("properties", {})
    return out


GENERIC: tuple[Generic, ...] = (
    Generic("get_my_access",
            "What this key can do right now: capabilities per target, where each "
            "limit is enforced, remaining budgets, role, expiries, delegation depth.",
            _NoArgs, lambda auth, a: agent.get_my_access(auth)),
    Generic("list_targets",
            "Enabled targets and the actions this key can reach on each.",
            _NoArgs, lambda auth, a: agent.list_targets(auth)),
    Generic("resolve_resource",
            "Look up resource ids by name inside what this key may see, e.g. "
            "target=whatsapp kind=chat query=alice.",
            _Resolve, lambda auth, a: agent.resolve_resource(auth, a.target, a.kind, a.query,
                                                             a.limit)),
    Generic("request_permission",
            "Ask the owner for more authority. capabilities = list of capability "
            "objects ({target, actions, selector?, constraints?, mode?, budget?}). "
            "Returns {id, status: pending}; a request beyond what your parent can "
            "give is refused with the clipped and allowed capabilities.",
            PermissionBody,
            lambda auth, a: agent.request_permission(auth, a.capabilities, a.reason,
                                                     a.expires_in_hours),
            read_only=False),
    Generic("get_permission_status", "One of this key's permission requests or grants.",
            _GrantId, lambda auth, a: agent.get_permission_status(auth, a.grant_id)),
    Generic("list_my_permissions", "This key's grants and requests, newest first.",
            _Page, lambda auth, a: agent.list_my_permissions(auth, a.limit, a.cursor)),
    Generic("delegate",
            "Mint a child key for a sub-agent, carved out of your own authority: you can "
            "only narrow (capabilities, role, rate and lifetime at most yours; your denies "
            "carry over). No human approval. Returns {key_id, name, key, expires_at, "
            "capabilities}; the key is shown once. A request beyond what you hold is "
            "refused with the clipped and allowed capabilities.",
            DelegateBody,
            lambda auth, a: delegation.delegate(auth, a.name, a.capabilities, a.reason,
                                                a.expires_in_hours, a.role, a.rate_per_min,
                                                a.denies),
            read_only=False, available=_can_delegate),
    Generic("list_my_delegations",
            "Keys you delegated directly: status, role, expiry and their grants.",
            _NoArgs, lambda auth, a: delegation.list_my_delegations(auth)),
    Generic("revoke_delegation",
            "Revoke a key you delegated (or one further down your delegation tree): it "
            "and every key below it stop working at once.",
            _KeyId, lambda auth, a: delegation.revoke_delegation(auth, a.key_id),
            read_only=False),
    Generic("get_action_status",
            "One of this key's queued actions: pending (awaiting approval), scheduled, "
            "sending, done (with result), rejected, expired, canceled or failed.",
            _ActionId, lambda auth, a: agent.get_action_status(auth, a.action_id)),
    Generic("list_my_actions", "This key's queued actions, newest first; filter by status.",
            _ActionPage, lambda auth, a: agent.list_my_actions(auth, a.status, a.limit,
                                                               a.cursor)),
    Generic("cancel_action", "Cancel one of this key's pending or scheduled actions.",
            _ActionId, lambda auth, a: agent.cancel_action(auth, a.action_id),
            read_only=False),
)

BY_NAME: dict[str, Generic] = {g.name: g for g in GENERIC}


def invalid_request(exc: ValidationError) -> PolicyError:
    """Same compact, input-free body as the REST 422 handler in main.py:
    pydantic's default message would echo the submitted values back."""
    parts = []
    for err in exc.errors()[:5]:
        loc = ".".join(str(x) for x in err.get("loc", ())) or "arguments"
        parts.append(f"{loc}: {err.get('msg', 'invalid')}")
    return PolicyError(422, "invalid request: " + "; ".join(parts), "invalid_request")


def call(auth, name: str, arguments: dict) -> Any:
    """Run generic tool `name` as `auth`. Blocking; call from a thread."""
    g = BY_NAME[name]
    try:
        args = g.args.model_validate(arguments)
    except ValidationError as exc:
        raise invalid_request(exc) from None
    return g.run(auth, args)
