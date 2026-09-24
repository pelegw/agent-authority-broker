"""The Telegram admin routes (console Channels view) and the poll-loop
supervisor: the bot token is write-only and encrypted, and storing or
clearing it starts or stops the poll loop at runtime, with no restart."""

import asyncio
import json
import threading
import time

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from broker import background, crypto, db
from broker.config import get_settings
from broker.notify import telegram as tg
from broker.notify import telegram_inbound as inbound

from .conftest import CSRF_HEADERS, TELEGRAM_TOKEN

OTHER_BOT = "987654321:BBothertokenothertokenothertoken02"
ROUTES = [("get", "/v1/admin/telegram"), ("post", "/v1/admin/telegram/token"),
          ("delete", "/v1/admin/telegram/token"), ("post", "/v1/admin/telegram/link/start"),
          ("post", "/v1/admin/telegram/enable"), ("post", "/v1/admin/telegram/disable"),
          ("post", "/v1/admin/telegram/test"), ("post", "/v1/admin/telegram/unlink")]


@pytest.fixture()
def no_network(monkeypatch):
    """Every Telegram call recorded, none made."""
    calls = []

    def api(method, _http_timeout=15.0, **payload):
        calls.append(method)
        return {"username": "aab_test_bot"} if method == "getMe" else {}
    monkeypatch.setattr(tg, "_api", api)
    monkeypatch.setattr(tg, "_api_send_message",
                        lambda text, keyboard=None: calls.append("sendMessage") or {})
    return calls


def _put_token(client, headers, token=TELEGRAM_TOKEN):
    return client.post("/v1/admin/telegram/token", headers=headers, json={"token": token})


def _audit_text():
    with db.connect() as conn:
        return json.dumps([dict(r) for r in conn.execute("SELECT * FROM audit_log")])


@pytest.mark.parametrize("method,path", ROUTES)
def test_every_route_requires_admin(client, owner, method, path):
    kw = {"json": {"token": TELEGRAM_TOKEN}} if path.endswith("/token") and method == "post" \
        else {}
    assert getattr(client, method)(path, **kw).status_code == 401
    assert crypto.state(crypto.BROKER_SLOT, tg.TOKEN_NAME) == "unset"


def test_token_is_write_only_and_encrypted(client, admin_headers, secrets_key, no_network):
    r = _put_token(client, admin_headers)
    assert r.status_code == 200, r.text
    assert TELEGRAM_TOKEN not in r.text
    body = client.get("/v1/admin/telegram", headers=admin_headers).json()
    assert body["token"] == "set" and body["bot_username"] == "aab_test_bot"
    assert TELEGRAM_TOKEN not in json.dumps(body)
    with db.connect() as conn:
        [row] = conn.execute("SELECT ciphertext FROM plugin_secrets").fetchall()
        cfg = json.dumps([dict(r) for r in conn.execute("SELECT * FROM app_config")])
    assert TELEGRAM_TOKEN.encode() not in bytes(row["ciphertext"])
    assert TELEGRAM_TOKEN not in cfg and TELEGRAM_TOKEN not in _audit_text()
    assert db.get_config(tg.CFG_BOT_ID) == "123456789"      # the public bot id only


def test_token_shape_is_enforced_without_echoing_it(client, admin_headers, secrets_key):
    for bad in ("not-a-token", "123:short", "12345678:AAtoken/../../x-evil-path-segment-xx",
                "123456789:AAtesttokentesttokentesttokentest01?x=1"):
        r = _put_token(client, admin_headers, bad)
        assert r.status_code == 400 and bad not in r.text, bad
    r = _put_token(client, admin_headers, "x" * 500)
    assert r.status_code == 422 and "xxxx" not in r.text
    assert crypto.state(crypto.BROKER_SLOT, tg.TOKEN_NAME) == "unset"


def test_token_needs_the_broker_secrets_key(client, admin_headers):
    r = _put_token(client, admin_headers)
    assert r.status_code == 409 and r.json()["code"] == "secrets_key_missing"
    assert crypto.state(crypto.BROKER_SLOT, tg.TOKEN_NAME) == "unset"


def test_delete_clears_the_token(client, admin_headers, secrets_key, no_network):
    _put_token(client, admin_headers)
    r = client.delete("/v1/admin/telegram/token", headers=admin_headers)
    assert r.status_code == 200 and r.json()["token"] == "unset"
    assert tg.desired_state() is None


def test_session_writes_need_the_csrf_header(session_client, secrets_key, no_network):
    assert _put_token(session_client, {}).status_code == 403
    assert _put_token(session_client, CSRF_HEADERS).status_code == 200


def test_a_different_bot_drops_the_link_and_offset(client, admin_headers, fake_telegram):
    db.set_config(tg.CFG_OFFSET, "77")
    _put_token(client, admin_headers, TELEGRAM_TOKEN)        # same bot: link kept
    assert tg.linked() and tg.enabled() and db.get_config(tg.CFG_OFFSET) == "77"
    r = _put_token(client, admin_headers, OTHER_BOT)
    assert r.json()["linked"] is False and r.json()["enabled"] is False
    assert db.get_config(tg.CFG_OFFSET) == "0"
    assert db.get_config(tg.CFG_BOT_ID) == "987654321"


def test_a_rotated_secrets_key_means_re_enter(client, admin_headers, fake_telegram,
                                              monkeypatch):
    monkeypatch.setenv("BROKER_SECRETS_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()
    body = client.get("/v1/admin/telegram", headers=admin_headers).json()
    assert body["token"] == "unreadable" and body["re_enter_required"] is True
    assert body["active"] is False and tg.desired_state() is None   # fail closed: off
    _put_token(client, admin_headers)
    assert client.get("/v1/admin/telegram", headers=admin_headers).json()["token"] == "set"


def test_link_enable_test_unlink_flow(client, admin_headers, secrets_key, no_network, owner):
    assert client.post("/v1/admin/telegram/link/start",
                       headers=admin_headers).json()["code"] == "no_token"
    _put_token(client, admin_headers)
    started = client.post("/v1/admin/telegram/link/start", headers=admin_headers).json()
    assert started["linking"] and len(started["code"]) == 16
    assert started["link"] == f"https://t.me/aab_test_bot?start={started['code']}"
    assert client.post("/v1/admin/telegram/enable", headers=admin_headers).status_code == 400
    assert client.post("/v1/admin/telegram/test", headers=admin_headers).status_code == 400
    inbound._handle_update({"update_id": 1, "message": {
        "chat": {"id": 555, "type": "private"}, "from": {"id": 555},
        "text": f"/start {started['code']}"}})
    assert tg.principal_id() == owner.id
    assert client.post("/v1/admin/telegram/enable", headers=admin_headers).json()["active"]
    assert client.post("/v1/admin/telegram/test", headers=admin_headers).json() == {"sent": True}
    assert client.post("/v1/admin/telegram/disable",
                       headers=admin_headers).json()["enabled"] is False
    out = client.post("/v1/admin/telegram/unlink", headers=admin_headers).json()
    assert out["linked"] is False and out["chat_id"] is None
    with db.connect() as conn:
        actions = [r["action"] for r in conn.execute(
            "SELECT action FROM audit_log WHERE action LIKE 'telegram.%' ORDER BY id")]
        vias = {r["actor_via"] for r in conn.execute(
            "SELECT actor_via FROM audit_log WHERE action LIKE 'telegram.%'")}
    assert actions == ["telegram.token_set", "telegram.link_started", "telegram.linked",
                       "telegram.enabled", "telegram.test", "telegram.disabled",
                       "telegram.unlinked"]
    assert vias <= {"token", "telegram"}


# ---- the supervisor: start/stop at runtime ---------------------------------------------

@pytest.fixture()
def fake_loop(monkeypatch):
    """Replace the real poll loop with one that records its life."""
    life = {"started": 0, "stopped": 0}

    async def loop():
        life["started"] += 1
        try:
            await asyncio.Event().wait()
        finally:
            life["stopped"] += 1
    monkeypatch.setattr(inbound, "poll_loop", loop)
    return life


def test_supervisor_starts_restarts_and_stops_the_loop(secrets_key, admin_ctx, fake_loop,
                                                       no_network):
    async def scenario():
        # Driven the way the app lifespan drives it (background.Loop).
        sup = background.Loop(inbound.supervise, 0.01)

        async def settle():
            await asyncio.sleep(0.1)

        await settle()
        assert fake_loop == {"started": 0, "stopped": 0}         # no token: no loop
        tg.set_token(admin_ctx, TELEGRAM_TOKEN)
        await settle()
        assert fake_loop == {"started": 1, "stopped": 0}
        tg.set_token(admin_ctx, OTHER_BOT)                       # a new token: restart
        await settle()
        assert fake_loop == {"started": 2, "stopped": 1}
        tg.clear_token(admin_ctx)
        await settle()
        assert fake_loop == {"started": 2, "stopped": 2}
        tg.set_token(admin_ctx, TELEGRAM_TOKEN)
        await settle()
        await sup.stop()
        assert fake_loop == {"started": 3, "stopped": 3}         # shutdown stops it too

    asyncio.run(scenario())


def test_supervisor_restarts_a_loop_that_died(secrets_key, admin_ctx, monkeypatch, no_network):
    runs = []

    async def dying():
        runs.append(1)
        raise RuntimeError("crashed")
    monkeypatch.setattr(inbound, "poll_loop", dying)
    tg.set_token(admin_ctx, TELEGRAM_TOKEN)

    async def scenario():
        sup = background.Loop(inbound.supervise, 0.01)
        await asyncio.sleep(0.1)
        await sup.stop()

    asyncio.run(scenario())
    assert len(runs) >= 2


def test_the_app_starts_and_stops_the_loop_without_a_restart(admin_headers, secrets_key,
                                                             fake_loop, no_network,
                                                             monkeypatch):
    monkeypatch.setattr(inbound, "SUPERVISE_INTERVAL", 0.02)
    from broker.main import app

    def wait_for(pred):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if pred():
                return True
            time.sleep(0.02)
        return False

    with TestClient(app) as c:
        assert _put_token(c, admin_headers).status_code == 200
        assert wait_for(lambda: fake_loop["started"] == 1)
        assert c.delete("/v1/admin/telegram/token", headers=admin_headers).status_code == 200
        assert wait_for(lambda: fake_loop["stopped"] == 1)
    assert fake_loop == {"started": 1, "stopped": 1}


def test_app_shutdown_waits_for_a_tap_in_flight(fake_telegram, monkeypatch):
    # Stopping the supervisor stops its poll loop the same way: a tap being
    # decided in a worker thread finishes before the app is down.
    monkeypatch.setattr(inbound, "SUPERVISE_INTERVAL", 0.02)
    started, finished = threading.Event(), threading.Event()

    def slow_handle(update):
        started.set()
        time.sleep(0.3)
        finished.set()

    monkeypatch.setattr(inbound, "_handle_update", slow_handle)
    fake_telegram["inject"]({"update_id": 1, "message": {"text": "hi"}})
    from broker.main import app
    with TestClient(app):
        assert started.wait(5)
    assert finished.is_set()
