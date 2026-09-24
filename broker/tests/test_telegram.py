"""Telegram approval channel: cards, callback dispatch, linking, the poll loop.

Ported from WA_GW tests/test_telegram.py onto the broker's paths: cards are
manifest-derived (echo's summary_template), approvals go through
services.admin with AdminContext(via="telegram") and are recorded as such,
and every WA_GW guarantee is kept: dual chat+user check, kill switch,
one-time link code from a private chat, oversized-card refusal, claim
conflicts answered "already handled". The link is now fail-closed: an
unset user id matches nobody (WA_GW let any member of the linked chat tap).
"""

import asyncio
import contextlib
import json
import logging

import pytest

from broker import auth, db, engine
from broker.actions import queue
from broker.authority import store
from broker.notify import cards
from broker.notify import telegram as tg
from broker.notify import telegram_inbound as inbound
from broker.plugins import settings

from .conftest import TELEGRAM_CHAT, TELEGRAM_TOKEN, TELEGRAM_USER, cap
from .test_no_admin_from_agent_paths import AGENT_ROOTS, graph


def _cb(update_id, data, chat_id=TELEGRAM_CHAT, from_id=TELEGRAM_USER, message_id=10):
    return {"update_id": update_id,
            "callback_query": {"id": f"cb{update_id}", "data": data, "from": {"id": int(from_id)},
                               "message": {"message_id": message_id,
                                           "chat": {"id": int(chat_id)}}}}


def _msg(update_id, chat_id, text="", chat_type="private", user_id=None, is_bot=False):
    m = {"message_id": update_id, "chat": {"id": chat_id, "type": chat_type},
         "from": {"id": chat_id if user_id is None else user_id, "is_bot": is_bot}}
    if text:
        m["text"] = text
    return {"update_id": update_id, "message": m}


def _audits(action):
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM audit_log WHERE action = ? ORDER BY id", (action,))]


@pytest.fixture()
def drafter(echo_local, make_agent):
    return make_agent([cap(["post_item"], mode="draft")], name="planner")


def draft(agent, text="hello", room="r1", **kw):
    r = engine.perform(agent.auth, "echo", "post_item", {"room": room, "text": text, **kw})
    assert r.status == 202
    return r.body["action_id"]


def status(action_id):
    return queue.get_row(action_id)["status"]


# ---- notifications ---------------------------------------------------------------------

def test_draft_creation_sends_a_manifest_card(drafter, fake_telegram):
    aid = draft(drafter, text="hello there", priority="high")
    [card] = fake_telegram["sent"]
    assert "Post to Room One: hello there" in card["text"]    # echo's summary_template
    assert "Echo" in card["text"] and "planner" in card["text"]
    # Everything the button approves is visible: params the summary doesn't show.
    assert "room: <code>r1</code>" in card["text"]
    assert "priority: <code>high</code>" in card["text"]
    assert "text:" not in card["text"]                         # already in the summary
    buttons = card["keyboard"]["inline_keyboard"][0]
    assert [b["callback_data"] for b in buttons] == [f"a:approve:{aid}", f"a:reject:{aid}"]
    assert all(len(b["callback_data"].encode()) <= 64 for b in buttons)


def test_card_shows_the_delegation_chain(echo_local, make_agent, owner, fake_telegram):
    parent = make_agent([cap(["post_item"], mode="draft")], name="planner")
    child = auth.create_key(owner.id, "researcher", "full", 60, None,
                            parent_key_id=parent.key_id, created_by="delegation")
    assert cards.key_label(child.key_id, "researcher") == "planner → researcher"
    assert cards.key_label(parent.key_id, "planner") == "planner"
    assert cards.key_label(99999, "gone") == "gone"
    text = cards.action_text({"id": "x", "target": "echo", "action": "post_item",
                              "key_id": child.key_id, "key_name": "researcher",
                              "summary": "Post to Room One: hi", "params": {}})
    assert "Key: <code>planner → researcher</code>" in text


def test_nothing_is_sent_unless_enabled_and_linked(drafter, fake_telegram):
    db.set_config(tg.CFG_ENABLED, "0")                       # token present, not enabled
    draft(drafter)
    db.set_config(tg.CFG_ENABLED, "1")
    db.set_config(tg.CFG_PRINCIPAL, "")                      # enabled, link incomplete
    draft(drafter)
    assert fake_telegram["sent"] == []


def test_notify_failure_is_non_fatal(drafter, fake_telegram, monkeypatch):
    def boom(*_a, **_k):
        raise tg.TelegramError(502, "down")
    monkeypatch.setattr(tg, "_api_send_message", boom)
    aid = draft(drafter)
    assert status(aid) == "pending"                          # the draft still exists
    [row] = _audits("notify.failed")
    assert json.loads(row["detail"])["error"] == "TelegramError"


def test_grant_request_card_shows_breadth_and_duration(client, echo_local, make_agent,
                                                       fake_telegram):
    a = make_agent([cap(["list_items"])], name="reader")
    r = client.post("/v1/permissions", headers=a.headers, json={
        "capabilities": [cap(["post_item"], selector={"room": ["r1"]})],
        "reason": "need to post", "expires_in_hours": 2})
    gid = r.json()["id"]
    [card] = fake_telegram["sent"]
    assert "Permission request" in card["text"] and "reader" in card["text"]
    assert "room: r1" in card["text"]
    assert "folder: <b>any</b>" in card["text"]              # unrestricted dimension, spelled out
    assert "for 2h" in card["text"] and "need to post" in card["text"]
    assert card["keyboard"]["inline_keyboard"][0][0]["callback_data"] == f"g:approve:{gid}"
    forever = cards.grant_text({"id": gid, "capabilities": [], "created_at": 1,
                                "expires_at": None, "key_id": a.key_id})
    assert "no expiry" in forever


# ---- callback dispatch ------------------------------------------------------------------

def test_tap_approves_and_records_telegram_as_the_surface(drafter, fake_telegram, owner):
    aid = draft(drafter, text="ok?")
    inbound._handle_update(_cb(1, f"a:approve:{aid}"))
    row = queue.get_row(aid)
    assert row["status"] == "done"
    assert (row["decided_via"], row["decided_by_principal"]) == ("telegram", owner.username)
    with db.connect() as conn:
        last = dict(conn.execute("SELECT * FROM decisions ORDER BY id DESC LIMIT 1").fetchone())
    assert (last["actor_via"], last["actor_principal"]) == ("telegram", owner.username)
    [approve] = _audits("action.approve")
    assert (approve["actor"], approve["actor_via"], approve["actor_principal"]) == \
        (owner.username, "telegram", owner.id)
    assert fake_telegram["answered"][-1]["text"] == "done"
    assert "Action done" in fake_telegram["edited"][-1]["text"]


def test_tap_rejects(drafter, fake_telegram):
    aid = draft(drafter)
    inbound._handle_update(_cb(1, f"a:reject:{aid}"))
    row = queue.get_row(aid)
    assert (row["status"], row["decided_via"]) == ("rejected", "telegram")


def test_tap_approves_a_grant(client, echo_local, make_agent, fake_telegram, owner):
    a = make_agent([cap(["list_items"])])
    gid = client.post("/v1/permissions", headers=a.headers, json={
        "capabilities": [cap(["post_item"], selector={"room": ["r1"]}, mode="direct")]}
                      ).json()["id"]
    inbound._handle_update(_cb(2, f"g:approve:{gid}"))
    g = store.get(gid)
    assert (g.status, g.decided_via, g.decided_by_principal) == ("active", "telegram",
                                                                 owner.username)
    assert "Permission approved" in fake_telegram["edited"][-1]["text"]
    assert client.post("/v1/targets/echo/actions/post_item", headers=a.headers,
                       json={"params": {"room": "r1", "text": "x"}}).status_code == 200


def test_tap_approving_a_scheduled_draft_schedules_it(drafter, fake_telegram):
    import time
    r = engine.perform(drafter.auth, "echo", "post_item", {"room": "r1", "text": "later"},
                       run_at=int(time.time()) + 3600)
    aid = r.body["action_id"]
    assert "Scheduled for" in fake_telegram["sent"][0]["text"]
    inbound._handle_update(_cb(1, f"a:approve:{aid}"))
    assert status(aid) == "scheduled"
    assert fake_telegram["answered"][-1]["text"] == "scheduled"


@pytest.mark.parametrize("chat_id,from_id", [(9999, TELEGRAM_USER), (TELEGRAM_CHAT, 999)])
def test_tap_from_the_wrong_chat_or_user_is_refused(drafter, fake_telegram, echo_local,
                                                   chat_id, from_id):
    aid = draft(drafter)
    inbound._handle_update(_cb(3, f"a:approve:{aid}", chat_id=chat_id, from_id=from_id))
    assert status(aid) == "pending"
    assert echo_local.impl.calls == []                        # nothing performed
    assert fake_telegram["answered"][-1]["text"] == "not authorized"
    assert len(_audits("telegram.rejected_chat")) == 1


def test_an_incomplete_link_matches_nobody(drafter, fake_telegram):
    aid = draft(drafter)
    db.set_config(tg.CFG_USER, "")                           # WA_GW treated this as "anyone"
    inbound._handle_update(_cb(3, f"a:approve:{aid}"))
    assert status(aid) == "pending"
    assert fake_telegram["answered"][-1]["text"] == "not authorized"


def test_a_disabled_or_missing_owner_cannot_approve(drafter, fake_telegram, owner):
    aid = draft(drafter)
    with db.connect() as conn:
        conn.execute("UPDATE principals SET disabled = 1 WHERE id = ?", (owner.id,))
    inbound._handle_update(_cb(3, f"a:approve:{aid}"))
    assert status(aid) == "pending"
    db.set_config(tg.CFG_PRINCIPAL, "no-such-principal")
    inbound._handle_update(_cb(4, f"a:approve:{aid}"))
    assert status(aid) == "pending"
    assert len(_audits("telegram.rejected_chat")) == 2


def test_kill_switch_refuses_taps(drafter, fake_telegram):
    aid = draft(drafter)
    db.set_config(tg.CFG_ENABLED, "0")
    inbound._handle_update(_cb(1, f"a:approve:{aid}"))
    assert status(aid) == "pending"
    assert fake_telegram["answered"][-1]["text"] == "approvals are disabled"


@pytest.mark.parametrize("data", ["garbage-no-colons", "a:approve:not-a-uuid", "x:approve:"
                                  "00000000-0000-0000-0000-000000000000", "a:revoke:"
                                  "00000000-0000-0000-0000-000000000000", None, 42])
def test_malformed_callbacks_are_answered_not_raised(fake_telegram, data):
    update = _cb(4, "placeholder")
    update["callback_query"]["data"] = data
    inbound._handle_update(update)
    assert fake_telegram["answered"][-1]["text"] == "bad request"


def test_console_first_then_tap_is_already_handled(client, admin_headers, drafter,
                                                   fake_telegram, echo_local):
    aid = draft(drafter, text="once")
    assert client.post(f"/v1/admin/actions/{aid}/approve", headers=admin_headers).status_code \
        == 200
    performed = len(echo_local.impl.calls)
    inbound._handle_update(_cb(5, f"a:approve:{aid}"))
    assert len(echo_local.impl.calls) == performed          # no second delivery
    assert fake_telegram["answered"][-1]["text"] == "already handled"
    assert "Action done" in fake_telegram["edited"][-1]["text"]   # shows what really happened


def test_double_tap_delivers_once(drafter, fake_telegram, echo_local):
    aid = draft(drafter)
    inbound._handle_update(_cb(1, f"a:approve:{aid}"))
    inbound._handle_update(_cb(2, f"a:approve:{aid}"))
    assert len([c for c in echo_local.impl.calls if c[0] == "post_item"]) == 1


def test_a_held_action_keeps_its_buttons(drafter, fake_telegram):
    aid = draft(drafter)
    settings.set_enabled("echo", False)
    inbound._handle_update(_cb(1, f"a:approve:{aid}"))
    assert status(aid) == "pending"
    assert "held" in fake_telegram["answered"][-1]["text"]
    assert fake_telegram["edited"] == []


def test_a_retryable_failure_keeps_its_buttons(drafter, fake_telegram, echo_local):
    aid = draft(drafter)
    echo_local.impl.fail_next = 503
    inbound._handle_update(_cb(1, f"a:approve:{aid}"))
    assert status(aid) == "pending"                          # released for a retry
    assert fake_telegram["edited"] == []
    inbound._handle_update(_cb(2, f"a:approve:{aid}"))
    assert status(aid) == "done"


# ---- oversized cards -------------------------------------------------------------------

def test_oversized_action_goes_to_the_console_and_cannot_be_approved(drafter, fake_telegram):
    aid = draft(drafter, text="x" * 4096)
    [msg] = fake_telegram["sent"]
    assert msg["keyboard"] is None                           # no approve button at all
    assert "console" in msg["text"] and aid in msg["text"]
    assert "x" * 100 not in msg["text"]                      # never a truncated body
    inbound._handle_update(_cb(1, f"a:approve:{aid}"))       # a forged/stale callback
    assert status(aid) == "pending"
    assert "console" in fake_telegram["answered"][-1]["text"]
    inbound._handle_update(_cb(2, f"a:reject:{aid}"))        # rejecting is always safe
    assert status(aid) == "rejected"


def test_the_bound_counts_utf16_units_not_characters(drafter, fake_telegram):
    # 1990 astral characters: the card is well under 4000 characters but
    # over 4000 UTF-16 code units, which is what Telegram counts.
    aid = draft(drafter, text="\U0001F600" * 1990)
    card_text = cards.action_text(cards.action_from_row(queue.get_row(aid)))
    assert len(card_text) < cards.MAX_UTF16 < cards.utf16_len(card_text)
    assert fake_telegram["sent"][0]["keyboard"] is None
    inbound._handle_update(_cb(1, f"a:approve:{aid}"))
    assert status(aid) == "pending"


def test_oversized_grant_goes_to_the_console(echo_local, make_agent, fake_telegram, owner):
    a = make_agent([cap(["list_items"])])
    rooms = [f"r{i}" for i in range(1, 1200)]
    from broker.authority.capability import from_json, normalize_all
    from broker.plugins.registry import get_registry
    caps = normalize_all([from_json(cap(["post_item"], selector={"room": rooms}))],
                         get_registry().manifests())
    g = store.insert_root_grant(owner.id, a.key_id, caps, "pending", "wide", None,
                                kind="expansion")
    card = cards.grant_card(cards.grant_from_store(g))
    assert not card.approvable and "console" in card.text
    inbound._handle_update(_cb(1, f"g:approve:{g.id}"))
    assert store.get(g.id).status == "pending"


# ---- linking -----------------------------------------------------------------------------

@pytest.fixture()
def unlinked(fake_telegram, admin_ctx):
    tg.unlink(admin_ctx)
    return fake_telegram


def test_linking_needs_the_code_from_a_private_chat(unlinked, admin_ctx, owner):
    assert not tg.status()["linked"]
    code = tg.start_linking(admin_ctx)["code"]
    inbound._handle_update(_msg(1, 9999))                                # no code
    inbound._handle_update(_msg(2, -7000, f"/start {code}", chat_type="group", user_id=333))
    inbound._handle_update(_msg(3, 7777, "/start wrongcode"))
    inbound._handle_update(_msg(4, 7777, f"/start {code}", user_id=8888))  # chat != sender
    inbound._handle_update(_msg(5, 7777, f"/start {code}", is_bot=True))
    assert not tg.linked()
    inbound._handle_update(_msg(6, 7777, f"/start {code}"))
    assert (tg.chat_id(), tg.user_id(), tg.principal_id()) == ("7777", "7777", owner.id)
    [row] = _audits("telegram.linked")
    assert (row["actor"], row["actor_principal"], row["actor_via"]) == \
        (owner.username, owner.id, "telegram")
    assert "linked" in unlinked["sent"][-1]["text"]


def test_a_link_code_is_single_use(unlinked, admin_ctx):
    code = tg.start_linking(admin_ctx)["code"]
    inbound._handle_update(_msg(1, 7777, f"/start {code}"))
    inbound._handle_update(_msg(2, 5555, f"/start {code}"))
    assert tg.chat_id() == "7777"


def test_an_expired_link_code_does_not_link(unlinked, admin_ctx, monkeypatch):
    code = tg.start_linking(admin_ctx)["code"]
    tg._link["expires"] = 0.0
    inbound._handle_update(_msg(1, 7777, f"/start {code}"))
    assert not tg.linked() and tg.pending_link() is None


def test_unlink_clears_the_link_and_disables(fake_telegram, admin_ctx):
    tg.unlink(admin_ctx)
    assert (tg.chat_id(), tg.user_id(), tg.principal_id(), tg.enabled()) == ("", "", "", False)
    assert len(_audits("telegram.unlinked")) == 1


# ---- poll loop ---------------------------------------------------------------------------

def _run(coro):
    async def go():
        with contextlib.suppress(asyncio.CancelledError):
            await coro
    asyncio.run(go())


def test_poll_loop_consumes_a_batch_and_persists_the_offset(drafter, fake_telegram,
                                                           monkeypatch):
    aid = draft(drafter, text="loop")
    calls = {"n": 0, "offsets": []}

    def get_updates(offset, timeout):
        calls["n"] += 1
        calls["offsets"].append(offset)
        if calls["n"] == 1:
            return [_cb(7, f"a:approve:{aid}"), {"update_id": "junk"}]
        raise asyncio.CancelledError()      # stop cleanly on the next poll
    monkeypatch.setattr(tg, "_get_updates", get_updates)
    _run(inbound.poll_loop())
    assert status(aid) == "done"
    assert db.get_config(tg.CFG_OFFSET) == "8"
    assert calls["offsets"] == [0, 8]
    assert "deleteWebhook" in fake_telegram["api"]
    assert tg.status()["poll"]["running"] is False


def test_poll_loop_resumes_from_the_persisted_offset(fake_telegram, monkeypatch):
    db.set_config(tg.CFG_OFFSET, "41")
    seen = []

    def get_updates(offset, timeout):
        seen.append(offset)
        raise asyncio.CancelledError()
    monkeypatch.setattr(tg, "_get_updates", get_updates)
    _run(inbound.poll_loop())
    assert seen == [41]


def test_poll_errors_back_off_and_are_audited_once_per_streak(fake_telegram, monkeypatch):
    sleeps, calls = [], {"n": 0}

    async def fake_sleep(s):
        sleeps.append(s)

    def get_updates(offset, timeout):
        calls["n"] += 1
        if calls["n"] <= 3:
            raise tg.TelegramError(502, f"bad gateway for bot{TELEGRAM_TOKEN}")
        if calls["n"] == 4:
            return []
        raise asyncio.CancelledError()
    monkeypatch.setattr(tg, "_get_updates", get_updates)
    monkeypatch.setattr(inbound.asyncio, "sleep", fake_sleep)
    _run(inbound.poll_loop())
    assert sleeps == [1, 2, 4]
    [err] = _audits("telegram.poll_error")
    assert TELEGRAM_TOKEN not in err["detail"]
    assert json.loads(err["detail"]) == {"error": "TelegramError", "status": 502}
    assert len(_audits("telegram.poll_recovered")) == 1
    assert tg.status()["poll"]["consecutive_errors"] == 0


def test_one_bad_update_does_not_stop_the_batch(drafter, fake_telegram, monkeypatch):
    aid = draft(drafter)
    real = inbound._handle_update

    def flaky(u):
        if u["update_id"] == 1:
            raise RuntimeError("boom")
        real(u)
    monkeypatch.setattr(inbound, "_handle_update", flaky)
    batches = iter([[_cb(1, "x"), _cb(2, f"a:approve:{aid}")]])

    def get_updates(offset, timeout):
        try:
            return next(batches)
        except StopIteration:
            raise asyncio.CancelledError() from None
    monkeypatch.setattr(tg, "_get_updates", get_updates)
    _run(inbound.poll_loop())
    assert status(aid) == "done"
    assert len(_audits("telegram.update_error")) == 1


# ---- structure and secrecy ----------------------------------------------------------------

def test_the_approve_path_is_unreachable_from_the_agent_surface():
    g = graph(AGENT_ROOTS)
    assert "broker.notify.telegram" in g                     # the outbound half is reachable...
    assert "broker.notify.telegram_inbound" not in g         # ...the tap handler is not
    assert "broker.services.admin" not in g
    outbound = graph(["broker.notify.telegram"])
    assert "broker.services.admin" not in outbound
    assert not [m for m in outbound if m.startswith("broker.identity")]


def test_the_token_never_reaches_audit_status_or_notify_errors(drafter, fake_telegram,
                                                                monkeypatch):
    def boom(*_a, **_k):
        raise tg.TelegramError(502, f"failed for {TELEGRAM_TOKEN}")
    monkeypatch.setattr(tg, "_api_send_message", boom)
    draft(drafter)
    assert TELEGRAM_TOKEN not in json.dumps(tg.status())
    with db.connect() as conn:
        dump = json.dumps([dict(r) for r in conn.execute("SELECT * FROM audit_log")])
        cfg = json.dumps([dict(r) for r in conn.execute("SELECT * FROM app_config")])
    assert TELEGRAM_TOKEN not in dump and TELEGRAM_TOKEN not in cfg


def test_api_errors_do_not_carry_the_url(secrets_key, monkeypatch):
    import httpx

    from broker import crypto
    crypto.put(crypto.BROKER_SLOT, tg.TOKEN_NAME, TELEGRAM_TOKEN)   # real _api, fake transport

    class Boom:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, path, json=None):
            req = httpx.Request("POST", f"https://api.telegram.org/bot{TELEGRAM_TOKEN}{path}")
            raise httpx.ReadTimeout(f"timed out calling {req.url}", request=req)
    monkeypatch.setattr(tg, "_client", lambda token, timeout: Boom())
    with pytest.raises(tg.TelegramError) as e:
        tg._api("getMe")
    assert TELEGRAM_TOKEN not in str(e.value) and e.value.__cause__ is None
    assert e.value.__suppress_context__


def test_httpx_request_logs_are_redacted(caplog):
    with caplog.at_level(logging.INFO, logger="httpx"):
        logging.getLogger("httpx").info('HTTP Request: %s %s "%s %d %s"', "POST",
                                        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getMe",
                                        "HTTP/1.1", 200, "OK")
    assert TELEGRAM_TOKEN not in caplog.text
    assert "/bot<redacted>/getMe" in caplog.text


def test_provider_is_registered_only_when_fully_live(env, fake_telegram, admin_ctx):
    from broker import notify
    assert tg in notify._providers()
    tg.set_enabled(admin_ctx, False)
    assert tg not in notify._providers()
    tg.set_enabled(admin_ctx, True)
    tg.clear_token(admin_ctx)
    assert tg not in notify._providers()


def test_a_truncating_template_never_hides_the_full_value(echo_local, monkeypatch):
    # The manifest validator checks placeholder names only, so "{text:.3}" is
    # legal; the card must then show the full text beside the button.
    import types
    act = types.SimpleNamespace(summary_template="Post {text:.3} to {room!r}")
    fake = types.SimpleNamespace(display_name="Echo", action=lambda name: act)
    monkeypatch.setattr(cards, "_manifest", lambda target: fake)
    text = cards.action_text({"id": "x", "target": "echo", "action": "post_item",
                              "key_id": None, "key_name": "k", "summary": "Post hel to 'r1'",
                              "params": {"text": "hello world", "room": "r1"}})
    assert "text: <code>hello world</code>" in text
    assert "room: <code>r1</code>" in text
    assert cards._direct_fields("{text} {text:.3} {room!r} {to_label}") == {"text", "to_label"}
