"""Long-poll bootstrap: a `long_poll` action called without its cursor is
"start from now" and is answered at once, whatever `?wait=` says. Holding it
would return the top of the feed at the END of the wait and silently skip
everything that arrived meanwhile. (The WhatsApp variant of these tests is in
tests/targets/test_whatsapp.py.)"""

import time

from broker import db, engine
from broker.plugins.registry import Registry

from .conftest import cap, echo_manifest

ACT = "/v1/targets/echo/actions"


def test_bootstrap_with_wait_returns_at_once(client, echo, make_agent):
    a = make_agent([cap(["watch"])])
    t0 = time.monotonic()
    r = client.get(f"{ACT}/watch", params={"wait": 5}, headers=a.headers)
    assert r.status_code == 200 and r.json() == {"cursor": 4, "items": []}
    # The default poll interval is 1 s: returning under it means no wait at all.
    assert time.monotonic() - t0 < 1.0


def test_nothing_arriving_after_bootstrap_is_skipped(client, echo, make_agent):
    a = make_agent([cap(["watch", "post_item"])])
    cursor = client.get(f"{ACT}/watch", params={"wait": 5}, headers=a.headers).json()["cursor"]
    client.post(f"{ACT}/post_item", json={"params": {"room": "r1", "text": "right after"}},
                headers=a.headers)
    r = client.get(f"{ACT}/watch", params={"cursor": cursor, "wait": 5}, headers=a.headers)
    assert [i["text"] for i in r.json()["items"]] == ["right after"]


def test_a_cursor_still_waits(client, echo_local, make_agent, monkeypatch):
    monkeypatch.setenv("LONG_POLL_INTERVAL_SECONDS", "0.05")
    from broker.config import get_settings
    get_settings.cache_clear()
    a = make_agent([cap(["watch"])])
    t0 = time.monotonic()
    r = client.get(f"{ACT}/watch", params={"cursor": 4, "wait": 1}, headers=a.headers)
    assert r.json() == {"cursor": 4, "items": []} and time.monotonic() - t0 >= 0.9


def test_bootstrap_is_still_recorded(client, echo_local, make_agent):
    a = make_agent([cap(["watch"])])
    client.get(f"{ACT}/watch", params={"wait": 5}, headers=a.headers)
    with db.connect() as conn:
        kinds = [(r["kind"], r["decision"] or r["outcome"]) for r in conn.execute(
            "SELECT kind, decision, outcome FROM decisions ORDER BY id")]
    assert kinds == [("decision", "allow"), ("outcome", "ok")]


def test_is_bootstrap_rule():
    echo = echo_manifest()
    whatsapp = Registry().vendored("whatsapp")
    assert engine.is_bootstrap(echo.action("watch"), {})
    assert not engine.is_bootstrap(echo.action("watch"), {"cursor": 0})   # 0 is a cursor
    assert engine.is_bootstrap(whatsapp.action("check_new_messages"), {"limit": 5})
    assert not engine.is_bootstrap(whatsapp.action("check_new_messages"), {"cursor": 9})
    # An action with no cursor param never bootstraps (it waits as asked).
    assert not engine.is_bootstrap(echo.action("list_items"), {})
    assert not engine.is_bootstrap(None, {})

