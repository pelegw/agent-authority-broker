"""Telegram approval cards: text plus inline keyboard, derived from manifests.

Pure rendering, no network. A card for a queued action is built from what
`notify_action` receives (`summary` from the manifest's summary_template,
`display_name`, the key) plus every parameter the summary does not show, so
the human sees everything the Approve button covers. A permission request's
card lists each capability with its real breadth ("any" for an unrestricted
dimension) and duration, and says so when the key's ceiling (its role) would
cap what the request asks for: approving then does not do what the agent
asked (writes still queue, or stay denied).

The oversized rule (ported from WA_GW): a card whose text exceeds Telegram's
limit is never truncated beside an Approve button. It is replaced by a
button-less message pointing to the console, and the inbound handler
re-renders the card at tap time and refuses an approve whose card would not
fit, so a forged or stale callback cannot approve what nobody could read.

Callback data is `a:{approve|reject}:{action id}` or `g:...:{grant id}`,
at most 46 bytes (Telegram's cap is 64), and is parsed strictly.
"""

from __future__ import annotations

import html
import json
import re
import string
import time
from dataclasses import dataclass

from .. import role_ceiling
from ..actions.queue import render_summary
from ..auth import key_chain
from ..authority.capability import from_json, to_json
from ..plugins.registry import get_registry

# Telegram's hard limit is 4096 characters after entity parsing; this bound
# is on the raw HTML (markup included) counted in UTF-16 code units, which
# is how Telegram counts, so it is conservative.
MAX_UTF16 = 4000

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_CALLBACK_RE = re.compile(rf"^(a|g):(approve|reject):({_UUID})$")
KINDS = {"a": "Action", "g": "Permission"}


@dataclass(frozen=True)
class Card:
    text: str
    keyboard: dict | None = None     # None = no buttons (review in the console)

    @property
    def approvable(self) -> bool:
        return self.keyboard is not None


def esc(value) -> str:
    return html.escape(str(value if value is not None else ""))


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def fits(text: str) -> bool:
    return utf16_len(text) <= MAX_UTF16


def keyboard(kind: str, item_id: str) -> dict:
    return {"inline_keyboard": [[
        {"text": "✅ Approve", "callback_data": f"{kind}:approve:{item_id}"},
        {"text": "❌ Reject", "callback_data": f"{kind}:reject:{item_id}"},
    ]]}


def parse_callback(data) -> tuple[str, str, str] | None:
    """(kind, verb, id) for well-formed callback data, else None."""
    m = _CALLBACK_RE.match(data) if isinstance(data, str) else None
    return (m.group(1), m.group(2), m.group(3)) if m else None


def key_label(key_id, fallback: str = "") -> str:
    """"planner → researcher" for a delegated key, the plain name for a root
    key. A broken chain falls back to the name the caller already had."""
    try:
        chain = key_chain(int(key_id)) if key_id is not None else []
    except (TypeError, ValueError):
        chain = []
    if chain:
        return " → ".join(row["name"] for row in chain)
    return fallback or (f"key {key_id}" if key_id is not None else "unknown key")


def _utc(ts: int) -> str:
    # UTC, explicitly labelled: the owner's phone may be in any zone.
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts))


def _value(v) -> str:
    return v if isinstance(v, str) else json.dumps(v, sort_keys=True, ensure_ascii=False)


def _direct_fields(template: str | None) -> set[str]:
    """Params a summary template shows verbatim, in full. A `<param>_label`
    placeholder shows a display label, not the id itself, so it does not
    count; neither does a field with a format spec or conversion, which the
    manifest validator allows and which can truncate (`{text:.20}`)."""
    if not template:
        return set()
    return {f for _, f, spec, conv in string.Formatter().parse(template)
            if f and not spec and not conv}


def _manifest(target: str):
    return get_registry().manifests().get(target)


def _redirect(kind: str, item_id: str, what: str) -> Card:
    return Card(f"🟡 <b>{what} waiting for you</b>\n"
                "It is too long to review here. Open the console to review the "
                f"complete request.\n{KINDS[kind]}: <code>{esc(item_id)}</code>")


# ---- queued actions ------------------------------------------------------------

def action_from_row(row: dict) -> dict:
    """Rebuild what notify_action received from an `actions` row, so the
    tap-time check renders the very card that was (or would have been) sent."""
    manifest = _manifest(row["target"])
    act = manifest.action(row["action"]) if manifest else None
    params = row["params"] if isinstance(row["params"], dict) else json.loads(row["params"])
    labels = {}
    if act is not None and act.selector_param and row.get("resource_label"):
        labels[f"{act.selector_param}_label"] = row["resource_label"]
    return {**row, "params": params,
            "summary": render_summary(act.summary_template if act else None, params, labels),
            "display_name": manifest.display_name if manifest else row["target"]}


def action_text(action: dict) -> str:
    manifest = _manifest(action["target"])
    act = manifest.action(action["action"]) if manifest else None
    summary = action.get("summary") or ""
    shown = _direct_fields(act.summary_template if act else None) if summary else set()
    params = action.get("params") or {}
    key = key_label(action.get("key_id"), action.get("key_name", ""))
    lines = [f"🟡 <b>Approve {esc(action.get('display_name') or action['target'])}: "
             f"{esc(action['action'])}?</b>",
             f"Key: <code>{esc(key)}</code>"]
    if summary:
        lines += ["", esc(summary)]
    rest = {k: v for k, v in sorted(params.items()) if k not in shown}
    if rest:
        lines += ["", "<b>Parameters</b>"]
        lines += [f"• {esc(k)}: <code>{esc(_value(v))}</code>" for k, v in rest.items()]
    if action.get("run_at"):
        lines += ["", f"🕒 Scheduled for {_utc(action['run_at'])}"]
    if action.get("note"):
        lines += ["", f"📝 {esc(action['note'])}"]
    return "\n".join(lines)


def action_card(action: dict) -> Card:
    text = action_text(action)
    if not fits(text):
        return _redirect("a", action["id"], "An action")
    return Card(text, keyboard("a", action["id"]))


# ---- permission requests ---------------------------------------------------------

def _dims_for(manifest, actions: list[str]) -> list[str]:
    """Selector dimensions that apply to any of these actions (not derived)."""
    if manifest is None:
        return []
    out = []
    for n in manifest.narrowings:
        if n.derived_from is not None:
            continue
        if n.applies_to == ["*"] or set(n.applies_to) & set(actions):
            out.append(n.dimension)
    return out


def _cap_lines(cap: dict) -> list[str]:
    manifest = _manifest(cap.get("target", ""))
    name = manifest.display_name if manifest else cap.get("target", "?")
    actions = list(cap.get("actions") or [])
    lines = [f"• <b>{esc(name)}</b>: {esc(', '.join(actions))}"]
    selector = cap.get("selector") or {}
    dims = _dims_for(manifest, actions)
    for dim in dims + sorted(d for d in selector if d not in dims):
        vals = selector.get(dim)
        shown = "<b>any</b>" if vals in (None, "*") else esc(", ".join(map(str, vals)))
        lines.append(f"   {esc(dim)}: {shown}")
    extras = [f"mode {esc(cap.get('mode') or 'direct')}"]
    for k, v in sorted((cap.get("constraints") or {}).items()):
        extras.append(f"{esc(k)}={esc(_value(v))}")
    for k, v in sorted((cap.get("budget") or {}).items()):
        extras.append(f"budget {esc(k)}={esc(v)}")
    if cap.get("expires_at"):
        extras.append(f"until {_utc(cap['expires_at'])}")
    lines.append("   " + "; ".join(extras))
    return lines


def _duration(grant: dict) -> str:
    exp = grant.get("expires_at")
    if not exp:
        return "<b>no expiry</b>"
    hours = max(1, round((exp - (grant.get("created_at") or exp)) / 3600))
    return f"for {hours}h (until {_utc(exp)})"


def grant_from_store(g) -> dict:
    """What notify_grant_request received, rebuilt from a stored Grant."""
    return {"id": g.id, "key_id": g.key_id, "reason": g.reason,
            "capabilities": [to_json(c) for c in g.capabilities],
            "created_at": g.created_at, "expires_at": g.expires_at}


def grant_text(grant: dict) -> str:
    caps = grant.get("capabilities") or []
    if isinstance(caps, str):
        caps = json.loads(caps)
    lines = ["🔐 <b>Permission request</b>",
             f"Key: <code>{esc(key_label(grant.get('key_id'), grant.get('key_name', '')))}</code>",
             "Wants:"]
    for cap in caps:
        lines += _cap_lines(cap)
    lines.append(f"Duration: {_duration(grant)}")
    if grant.get("reason"):
        lines.append(f"Reason: {esc(grant['reason'])}")
    ceiling = _ceiling_note(grant, caps)
    if ceiling:
        lines += ["", f"⚠️ {esc(ceiling)}"]
    return "\n".join(lines)


def _ceiling_note(grant: dict, caps: list) -> str | None:
    """role_ceiling.note for the requesting key's chain, or None. A broken
    key chain (that key cannot authenticate) or a capability that does not
    parse claims nothing either way."""
    try:
        chain = key_chain(int(grant.get("key_id")))
        parsed = [from_json(c) for c in caps]
    except (TypeError, ValueError):
        return None
    ceiling = role_ceiling.key_ceiling(row["role"] for row in chain)
    if ceiling is None:
        return None
    return role_ceiling.note(
        ceiling, role_ceiling.grant_lowered(get_registry().manifests(), parsed, ceiling))


def grant_card(grant: dict) -> Card:
    text = grant_text(grant)
    if not fits(text):
        return _redirect("g", grant["id"], "A permission request")
    return Card(text, keyboard("g", grant["id"]))


# ---- outcomes -----------------------------------------------------------------------

_OUTCOME_ICONS = {"done": "✅", "approved": "✅", "scheduled": "🕒",
                  "rejected": "❌", "revoked": "❌"}


def outcome_text(kind: str, status: str, item_id: str = "") -> str:
    status = "approved" if status == "active" else status
    icon = _OUTCOME_ICONS.get(status, "•")
    tail = f"\n<code>{esc(item_id)}</code>" if item_id else ""
    return f"{icon} <b>{KINDS.get(kind, 'Request')} {esc(status)}</b>{tail}"
