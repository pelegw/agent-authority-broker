"""JID normalization: parity with the Go sidecar, WA_GW's cases, and the one
deliberate tightening (canonical user parts only)."""

import re
from pathlib import Path

import pytest

from aab_plugin_runtime import AdapterError
from aab_plugin_whatsapp.jid import normalize_jid

ALICE = "972501111111@s.whatsapp.net"

# Copied from sidecars/whatsapp/internal/wa/actions_test.go (TestParseRecipient),
# in the same order. If that table changes, change this one with it: the
# broker compares grants against what this function returns and the sidecar
# delivers to what ParseRecipient returns, so they must agree.
GO_VECTORS = [
    ("972501234567@s.whatsapp.net", "972501234567@s.whatsapp.net", False),
    ("12345-67890@g.us", "12345-67890@g.us", False),
    ("123456789012345@lid", "123456789012345@lid", False),                     # hidden-user chats
    ("972501234567:12@s.whatsapp.net", "972501234567@s.whatsapp.net", False),  # device suffix stripped
    ("123456789012345:9@lid", "123456789012345@lid", False),                   # lid device suffix stripped
    ("972501234567.0:2@s.whatsapp.net", "972501234567@s.whatsapp.net", False), # agent+device stripped
    ("@lid", "", True),  # empty user must be rejected
    ("@s.whatsapp.net", "", True),
    ("+972501234567", "972501234567@s.whatsapp.net", False),
    ("972501234567", "972501234567@s.whatsapp.net", False),
    (" +972501234567 ", "972501234567@s.whatsapp.net", False),  # whitespace tolerated
    ("not-a-number", "", True),
    ("", "", True),
    ("hello@example.com", "", True),  # wrong server
]


@pytest.mark.parametrize("raw,want,want_err", GO_VECTORS)
def test_parity_with_go_parse_recipient(raw, want, want_err):
    if want_err:
        with pytest.raises(AdapterError) as e:
            normalize_jid(raw)
        assert e.value.status == 400
    else:
        assert normalize_jid(raw) == want


def test_go_vectors_match_the_go_test_file():
    """Guard against the two tables drifting: every input in the Go test
    appears here with the same expectation."""
    go = (Path(__file__).resolve().parents[3] / "sidecars" / "whatsapp" / "internal" / "wa"
          / "actions_test.go")
    if not go.is_file():
        pytest.skip("sidecar sources not present")
    text = go.read_text(encoding="utf-8")
    rows = re.findall(r'\{\s*"((?:[^"\\]|\\.)*)",\s*"((?:[^"\\]|\\.)*)",\s*(true|false)\s*\}', text)
    assert rows, "could not parse the Go vectors"
    assert [(i, w, e == "true") for i, w, e in rows] == GO_VECTORS


# ---- WA_GW's own cases (tests/test_policy.py) ------------------------------------

def test_normalize_jid_accepts_phone_and_jids():
    assert normalize_jid("+972501111111") == ALICE
    assert normalize_jid("972501111111") == ALICE
    assert normalize_jid(ALICE) == ALICE
    assert normalize_jid("123@g.us") == "123@g.us"


def test_normalize_jid_lid_and_device_suffix():
    assert normalize_jid("123456789012345@lid") == "123456789012345@lid"
    assert normalize_jid("972501111111:12@s.whatsapp.net") == ALICE
    assert normalize_jid("972501111111.0@s.whatsapp.net") == ALICE
    assert normalize_jid("972501111111.0:2@s.whatsapp.net") == ALICE
    assert normalize_jid("123456789012345:9@lid") == "123456789012345@lid"


@pytest.mark.parametrize("bad", ["", "not-a-number", "x@example.com", "@s.whatsapp.net"])
def test_normalize_jid_rejects_garbage(bad):
    with pytest.raises(AdapterError) as e:
        normalize_jid(bad)
    assert e.value.status == 400


# ---- the tightening: one canonical spelling per chat -----------------------------

@pytest.mark.parametrize("alias", [
    "+972501111111@s.whatsapp.net",       # '+' inside a JID
    "97250 1111111@s.whatsapp.net",       # inner whitespace
    "972501111111​@s.whatsapp.net",  # zero-width space
    "９７２501111111@s.whatsapp.net",      # full-width digits
    "abc@s.whatsapp.net",
    "1-2-3@g.us",
])
def test_non_canonical_user_parts_are_refused(alias):
    # Grants and hidden lists compare exact strings; an alias the servers
    # might route to the same account must never get through as a new name.
    with pytest.raises(AdapterError) as e:
        normalize_jid(alias)
    assert e.value.status == 400


@pytest.mark.parametrize("value", [None, 972501111111, ["x"]])
def test_non_string_is_400(value):
    with pytest.raises(AdapterError) as e:
        normalize_jid(value)
    assert e.value.status == 400
