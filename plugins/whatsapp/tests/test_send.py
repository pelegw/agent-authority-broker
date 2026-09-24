"""send_message: normalization, the scope check before the sidecar, and the
503 / 502 outcomes. Ported from WA_GW tests/test_send_drafts.py's sidecar
cases (routing and drafts now live in the broker)."""

import httpx
import pytest

from .conftest import scope
from .fakes import ALICE, BOB, CAROL


def test_send_normalizes_the_recipient(perform, sidecar):
    r = perform("send_message", {"to": "+972501111111", "text": "hi"})
    assert r.status_code == 200
    assert r.json()["data"] == {"status": "sent", "message_id": "MSG1", "ts": 1700000000}
    assert sidecar.sent == [(ALICE, "hi")]


def test_send_to_a_chat_the_archive_has_never_seen(perform, sidecar):
    # A first message to someone is legitimate: this is an id check, not an
    # archive lookup.
    assert perform("send_message", {"to": CAROL, "text": "hello"}).status_code == 200
    assert sidecar.sent == [(CAROL, "hello")]


def test_send_result_carries_no_resource_ref(perform):
    # A post-filter 404 on a result would tell the agent an already-sent
    # message was not sent.
    data = perform("send_message", {"to": ALICE, "text": "hi"}).json()["data"]
    assert "resource_ref" not in data


@pytest.mark.parametrize("call_scope", [scope(deny=[ALICE]), scope(allow_only=[BOB]),
                                        scope(allow_only=[]),
                                        scope(deny=[ALICE], allow_only=[ALICE])])
def test_out_of_scope_recipient_never_reaches_the_sidecar(perform, sidecar, call_scope):
    r = perform("send_message", {"to": ALICE, "text": "x"}, call_scope)
    assert r.status_code == 404 and r.json() == {"error": "no such chat"}
    assert sidecar.sent == [] and sidecar.requests == []


def test_device_suffix_alias_of_a_hidden_chat_is_refused(perform, sidecar):
    r = perform("send_message", {"to": "972501111111:3@s.whatsapp.net", "text": "x"},
                scope(deny=[ALICE]))
    assert r.status_code == 404 and sidecar.sent == []


@pytest.mark.parametrize("params", [{"to": "not-a-number", "text": "x"},
                                    {"to": "+972501111111@s.whatsapp.net", "text": "x"},
                                    {"to": ALICE, "text": ""}, {"to": ALICE},
                                    {"text": "x"}, {"to": ALICE, "text": 7}])
def test_bad_input_is_400_before_the_sidecar(perform, sidecar, params):
    assert perform("send_message", params).status_code == 400
    assert sidecar.requests == []


def test_whitespace_text_is_sent_as_is(perform, sidecar):
    assert perform("send_message", {"to": ALICE, "text": "  spaced  "}).status_code == 200
    assert sidecar.sent == [(ALICE, "  spaced  ")]


def test_not_linked_is_503_and_nothing_sent(perform, sidecar):
    sidecar.paired(False)
    r = perform("send_message", {"to": ALICE, "text": "x"})
    assert r.status_code == 503 and sidecar.sent == []


def test_unreachable_sidecar_is_503(perform, sidecar):
    sidecar.fail_next["/send"] = (httpx.ConnectError, False)
    assert perform("send_message", {"to": ALICE, "text": "x"}).status_code == 503
    assert sidecar.sent == []


def test_timeout_after_sending_is_502(perform, sidecar):
    # The message went out and the answer was lost: unknown outcome, never
    # retried automatically by the broker.
    sidecar.fail_next["/send"] = (httpx.ReadTimeout, True)
    assert perform("send_message", {"to": ALICE, "text": "x"}).status_code == 502
    assert sidecar.sent == [(ALICE, "x")]


def test_sidecar_502_passes_through(perform, sidecar):
    sidecar.fail_next["/send"] = (502, False)
    assert perform("send_message", {"to": ALICE, "text": "x"}).status_code == 502


def test_sidecar_500_is_unknown_outcome(perform, sidecar):
    sidecar.fail_next["/send"] = (500, True)
    r = perform("send_message", {"to": ALICE, "text": "x"})
    assert r.status_code == 502 and sidecar.sent == [(ALICE, "x")]
