"""aab_ agent keys: hashing, rotation grace, expiry, disabling, the parent
chain, the depth cap, merged denies, and the current_auth dependency.
Ported from WA_GW tests/test_security.py key-lifecycle tests."""

import time

import pytest

from broker import auth, db
from broker.errors import PolicyError

from .helpers import bearer


def _set(key_id, **cols):
    with db.connect() as conn:
        for col, val in cols.items():
            conn.execute(f"UPDATE api_keys SET {col} = ? WHERE id = ?", (val, key_id))


def test_key_format_and_only_hash_is_stored(key_factory):
    k = key_factory(name="alpha")
    assert k.plaintext.startswith("aab_") and len(k.plaintext) == 4 + 48
    assert k.plaintext not in repr(k)
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM api_keys WHERE id = ?", (k.key_id,)).fetchone()
    assert row["key_hash"] == auth.hash_key(k.plaintext)
    assert k.plaintext not in [str(v) for v in dict(row).values()]


def test_authenticate_returns_context(key_factory, principal):
    k = key_factory(name="alpha", role="read-draft", rate_per_min=9)
    ctx = auth.authenticate_bearer(bearer(k.plaintext), "1.2.3.4")
    assert ctx.key_id == k.key_id and ctx.principal_id == principal
    assert (ctx.name, ctx.role, ctx.rate_per_min, ctx.depth) == ("alpha", "read-draft", 9, 0)
    assert ctx.parent_key_id is None and ctx.chain_key_ids == (k.key_id,)


@pytest.mark.parametrize("header", [None, "", "aab_x", "Bearer ", "Bearer wagw_abc",
                                    "Basic aab_abc", "Bearer aab_" + "0" * 48,
                                    "Bearer aab_\xff\xfe"])
def test_bad_headers_are_rejected(env, header):
    assert auth.authenticate_bearer(header) is None


def test_admin_token_shape_is_never_an_agent_key(key_factory):
    k = key_factory()
    # Even if a row somehow had the hash of an aab_admin_ string, the prefix
    # check refuses it before any lookup.
    admin_like = "aab_admin_" + "a" * 48
    _set(k.key_id, key_hash=auth.hash_key(admin_like))
    assert auth.authenticate_bearer(bearer(admin_like)) is None


def test_expired_key_is_rejected(key_factory):
    k = key_factory()
    _set(k.key_id, expires_at=int(time.time()) - 1)
    assert auth.authenticate_bearer(bearer(k.plaintext)) is None


def test_create_with_past_expiry_refused(principal):
    with pytest.raises(ValueError):
        auth.create_key(principal, "old", "full", 5, int(time.time()) - 5)


def test_disabled_key_is_rejected(key_factory):
    k = key_factory()
    assert auth.disable_key(k.key_id)
    assert auth.authenticate_bearer(bearer(k.plaintext)) is None
    assert auth.disable_key(99999) is False


def test_rotation_keeps_old_key_during_grace_then_kills_it(key_factory):
    k = key_factory()
    new = auth.rotate_key(k.key_id, grace_seconds=3600)
    assert new != k.plaintext and new.startswith("aab_")
    assert auth.authenticate_bearer(bearer(new)) is not None
    old_ctx = auth.authenticate_bearer(bearer(k.plaintext))
    assert old_ctx is not None
    # Using the previous secret reports the grace end as the credential expiry.
    assert old_ctx.credential_expires_at is not None
    assert auth.authenticate_bearer(bearer(new)).credential_expires_at is None
    _set(k.key_id, prev_expires_at=int(time.time()) - 1)
    assert auth.authenticate_bearer(bearer(k.plaintext)) is None
    assert auth.authenticate_bearer(bearer(new)) is not None


def test_rotation_default_grace_and_missing_key(key_factory):
    k = key_factory()
    auth.rotate_key(k.key_id)
    with db.connect() as conn:
        row = conn.execute("SELECT prev_expires_at FROM api_keys WHERE id = ?",
                           (k.key_id,)).fetchone()
    assert row["prev_expires_at"] > int(time.time()) + 86000
    with pytest.raises(KeyError):
        auth.rotate_key(424242)


def test_disabling_kills_both_secrets_during_grace(key_factory):
    k = key_factory()
    new = auth.rotate_key(k.key_id, 3600)
    auth.disable_key(k.key_id)
    assert auth.authenticate_bearer(bearer(new)) is None
    assert auth.authenticate_bearer(bearer(k.plaintext)) is None


def test_last_used_is_tracked(key_factory):
    k = key_factory()
    auth.authenticate_bearer(bearer(k.plaintext), "203.0.113.9")
    with db.connect() as conn:
        row = conn.execute("SELECT last_used_at, last_used_ip FROM api_keys WHERE id = ?",
                           (k.key_id,)).fetchone()
    assert row["last_used_at"] is not None and row["last_used_ip"] == "203.0.113.9"


def test_disabled_or_missing_principal_rejects(key_factory, principal):
    k = key_factory()
    with db.connect() as conn:
        conn.execute("UPDATE principals SET disabled = 1 WHERE id = ?", (principal,))
    assert auth.authenticate_bearer(bearer(k.plaintext)) is None
    with pytest.raises(ValueError):
        auth.create_key(principal, "x", "full", 5, None)
    with pytest.raises(ValueError):
        auth.create_key("nobody", "y", "full", 5, None)


def test_create_key_validation(key_factory, principal):
    key_factory(name="dup")
    with pytest.raises(ValueError):
        key_factory(name="dup")
    for kwargs in ({"role": "read-send"}, {"rate_per_min": 0}, {"name": "  "}):
        with pytest.raises(ValueError):
            key_factory(**kwargs)
    with pytest.raises(ValueError):
        auth.create_key(principal, "cb", "full", 5, None, created_by="admin")
    with pytest.raises(ValueError):
        key_factory(denies={"whatsapp": {"chat": "not-a-list"}})


# ---- parent chain --------------------------------------------------------------------

def test_child_key_authenticates_with_chain(key_factory):
    root = key_factory(name="root", role="full", rate_per_min=30)
    child = key_factory(name="child", role="read-act", rate_per_min=10, parent=root.key_id)
    ctx = auth.authenticate_bearer(bearer(child.plaintext))
    assert ctx.depth == 1 and ctx.parent_key_id == root.key_id
    assert ctx.chain_key_ids == (root.key_id, child.key_id)
    assert ctx.chain_roles == ("full", "read-act")
    assert [r["id"] for r in auth.key_chain(child.key_id)] == [root.key_id, child.key_id]


@pytest.mark.parametrize("breakage", ["disabled", "expired", "deleted", "other_principal"])
def test_any_broken_ancestor_kills_descendants(key_factory, breakage):
    root = key_factory(name="root")
    mid = key_factory(name="mid", parent=root.key_id)
    leaf = key_factory(name="leaf", parent=mid.key_id)
    assert auth.authenticate_bearer(bearer(leaf.plaintext)) is not None
    if breakage == "disabled":
        auth.disable_key(root.key_id)
    elif breakage == "expired":
        _set(mid.key_id, expires_at=int(time.time()) - 1)
    elif breakage == "deleted":
        with db.connect() as conn:
            conn.execute("DELETE FROM api_keys WHERE id = ?", (mid.key_id,))
    else:
        _set(root.key_id, principal_id="someone-else")
    assert auth.authenticate_bearer(bearer(leaf.plaintext)) is None


def test_parent_loop_fails_closed(key_factory):
    a = key_factory(name="a")
    b = key_factory(name="b", parent=a.key_id)
    _set(a.key_id, parent_key_id=b.key_id)          # corrupt: a <-> b
    assert auth.key_chain(b.key_id) == []
    assert auth.authenticate_bearer(bearer(b.plaintext)) is None


def test_depth_cap_on_create_and_on_auth(key_factory, monkeypatch):
    keys = [key_factory(name="d0")]
    for i in range(1, 4):                            # max_delegation_depth = 3
        keys.append(key_factory(name=f"d{i}", parent=keys[-1].key_id))
    assert auth.authenticate_bearer(bearer(keys[-1].plaintext)).depth == 3
    with pytest.raises(ValueError, match="depth"):
        key_factory(name="d4", parent=keys[-1].key_id)
    # Lowering the cap later makes existing deep keys stop authenticating.
    monkeypatch.setenv("MAX_DELEGATION_DEPTH", "2")
    from broker.config import get_settings
    get_settings.cache_clear()
    assert auth.authenticate_bearer(bearer(keys[-1].plaintext)) is None
    assert auth.authenticate_bearer(bearer(keys[2].plaintext)) is not None


def test_child_cannot_exceed_parent_role_rate_or_expiry(key_factory):
    exp = int(time.time()) + 3600
    root = key_factory(name="root", role="read-draft", rate_per_min=10, expires_at=exp)
    with pytest.raises(ValueError, match="role"):
        key_factory(name="c1", role="full", parent=root.key_id, expires_at=exp)
    with pytest.raises(ValueError, match="rate"):
        key_factory(name="c2", role="read-only", rate_per_min=11, parent=root.key_id,
                    expires_at=exp)
    with pytest.raises(ValueError, match="outlive"):
        key_factory(name="c3", role="read-only", rate_per_min=5, parent=root.key_id)
    with pytest.raises(ValueError, match="outlive"):
        key_factory(name="c4", role="read-only", rate_per_min=5, parent=root.key_id,
                    expires_at=exp + 1)
    ok = key_factory(name="c5", role="read-only", rate_per_min=5, parent=root.key_id,
                     expires_at=exp - 10)
    assert auth.authenticate_bearer(bearer(ok.plaintext)).expires_at == exp - 10


def test_cannot_delegate_from_dead_parent(key_factory):
    root = key_factory(name="root")
    auth.disable_key(root.key_id)
    with pytest.raises(ValueError, match="disabled"):
        key_factory(name="c", parent=root.key_id)


def test_expires_at_is_earliest_along_chain(key_factory):
    exp = int(time.time()) + 100
    root = key_factory(name="root", expires_at=exp)
    child = key_factory(name="child", parent=root.key_id, expires_at=exp)
    assert auth.authenticate_bearer(bearer(child.plaintext)).expires_at == exp


def test_denies_are_merged_along_chain(key_factory):
    root = key_factory(name="root", denies={"whatsapp": {"chat": ["A"]}})
    child = key_factory(name="child", parent=root.key_id,
                        denies={"whatsapp": {"chat": ["B"]}, "github": {"repo": ["o/r"]}})
    ctx = auth.authenticate_bearer(bearer(child.plaintext))
    assert ctx.denies == {"whatsapp": {"chat": ["A", "B"]}, "github": {"repo": ["o/r"]}}


def test_corrupt_denies_fail_closed(key_factory):
    k = key_factory()
    _set(k.key_id, denies="{not json")
    assert auth.authenticate_bearer(bearer(k.plaintext)) is None


# ---- FastAPI dependency ------------------------------------------------------------------

class _Req:
    def __init__(self):
        self.scope = {"state": {"client_ip": "198.51.100.1"}}
        self.client = None


def test_current_auth_dependency(key_factory):
    from broker.deps import current_auth
    k = key_factory()
    assert current_auth(_Req(), bearer(k.plaintext)).key_id == k.key_id
    # PolicyError, so the agent surface answers with the compact
    # {"error", "code"} body like every other refusal.
    with pytest.raises(PolicyError) as e:
        current_auth(_Req(), "Bearer aab_nope")
    assert e.value.status == 401 and e.value.code == "unauthorized"
    with pytest.raises(PolicyError):
        current_auth(_Req(), "Bearer aab_\xff")
