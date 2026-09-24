"""The decision record: append-only, hash-chained accountability log.

Every evaluation the engine makes is recorded here BEFORE any side effect
(denies included), and every performed call adds an `outcome` row linked by
`request_id`. Params are never stored, only `params_hash`.

Chain: each row's `hash` covers the previous row's hash plus the row's
canonical JSON (sorted keys, compact separators, the row id included):

    hash = HMAC-SHA256(DECISION_SIGNING_KEY, prev_hash + "\n" + canonical)   signed=1
    hash = SHA-256(prev_hash + "\n" + canonical)                              signed=0

The unsigned form exists only so a broker without a signing key still gets
tamper *evidence* against accidental edits; with the key, forging a row
needs the key. Appends run inside `BEGIN IMMEDIATE`, reading `MAX(id)`, so
two writers can never both chain onto the same predecessor.

`verify()` recomputes the chain. Downgrade guard: once a signed row exists,
every later row must be signed too, or an attacker with DB access could
rewrite the tail as unsigned SHA-256 rows and still verify.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

from . import db
from .config import get_settings

GENESIS = "0" * 64
_CHAIN_FIELDS = ("id", "request_id", "kind", "ts", "principal_id", "key_id", "key_name",
                 "grant_chain", "target", "action", "resource", "params_hash", "decision",
                 "reason", "enforced_where", "outcome", "actor_principal", "actor_via", "signed")


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def params_hash(params: Any) -> str:
    return hashlib.sha256(canonical(params or {}).encode()).hexdigest()


def _key() -> bytes | None:
    k = get_settings().decision_signing_key
    return k.encode() if k else None


def _digest(prev_hash: str, canon: str, signed: bool, key: bytes | None) -> str:
    msg = (prev_hash + "\n" + canon).encode()
    if signed:
        return hmac.new(key or b"", msg, hashlib.sha256).hexdigest()
    return hashlib.sha256(msg).hexdigest()


def _canon_row(row: dict) -> str:
    d = {f: row.get(f) for f in _CHAIN_FIELDS}
    # JSON columns are chained as values, not as their stored text, so the
    # hash does not depend on how a JSON column happens to be serialized.
    for col in ("grant_chain", "enforced_where"):
        if isinstance(d[col], str):
            d[col] = json.loads(d[col])
    return canonical(d)


def record(*, request_id: str, kind: str, target: str, action: str, auth=None,
           grant_chain: list[str] | tuple[str, ...] = (), resource: str = "",
           params: Any = None, decision: str | None = None, reason: str = "",
           enforced_where: dict | None = None, outcome: str | None = None,
           actor_principal: str | None = None, actor_via: str | None = None,
           principal_id: str | None = None, key_id: int | None = None,
           key_name: str | None = None, p_hash: str | None = None) -> int:
    """Append one row; returns its id."""
    if kind not in ("decision", "outcome"):
        raise ValueError("kind must be decision or outcome")
    key = _key()
    row = {
        "request_id": request_id, "kind": kind, "ts": int(time.time()),
        "principal_id": principal_id if auth is None else auth.principal_id,
        "key_id": key_id if auth is None else auth.key_id,
        "key_name": key_name if auth is None else auth.name,
        "grant_chain": list(grant_chain), "target": target, "action": action,
        "resource": resource or "",
        "params_hash": p_hash if p_hash is not None else (
            params_hash(params) if params is not None else ""),
        "decision": decision, "reason": reason or "",
        "enforced_where": dict(enforced_where or {}), "outcome": outcome,
        "actor_principal": actor_principal, "actor_via": actor_via,
        "signed": 1 if key else 0,
    }
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            last = conn.execute(
                "SELECT id, hash FROM decisions ORDER BY id DESC LIMIT 1").fetchone()
            row["id"] = (last["id"] + 1) if last else 1
            prev = last["hash"] if last else GENESIS
            digest = _digest(prev, _canon_row(row), bool(key), key)
            conn.execute(
                "INSERT INTO decisions (id, request_id, kind, ts, principal_id, key_id, key_name,"
                " grant_chain, target, action, resource, params_hash, decision, reason,"
                " enforced_where, outcome, actor_principal, actor_via, prev_hash, hash, signed)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (row["id"], request_id, kind, row["ts"], row["principal_id"], row["key_id"],
                 row["key_name"], canonical(row["grant_chain"]), target, action,
                 row["resource"], row["params_hash"], decision, row["reason"],
                 canonical(row["enforced_where"]), outcome, actor_principal, actor_via,
                 prev, digest, row["signed"]))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()
    return row["id"]


def verify(from_id: int | None = None) -> dict:
    """Recompute the chain. Returns {ok, checked, first_bad_id, signed}.

    `signed` is True when every checked row was HMAC-signed and verified
    with the configured key. A signed row cannot be verified without the
    key, so it is reported bad rather than skipped (fail closed).
    """
    key = _key()
    conn = db.connect()
    try:
        prev = GENESIS
        if from_id is not None and from_id > 1:
            before = conn.execute("SELECT hash FROM decisions WHERE id < ? ORDER BY id DESC"
                                  " LIMIT 1", (from_id,)).fetchone()
            prev = before["hash"] if before else GENESIS
        rows = conn.execute("SELECT * FROM decisions WHERE id >= ? ORDER BY id",
                            (from_id or 0,)).fetchall()
    finally:
        conn.close()
    checked, all_signed, seen_signed = 0, True, False
    for r in rows:
        row = dict(r)
        checked += 1
        signed = row["signed"] == 1
        bad = row["prev_hash"] != prev
        if signed:
            seen_signed = True
            bad = bad or key is None
        elif seen_signed:
            bad = True                    # downgrade after a signed row
        if not bad:
            try:
                expected = _digest(prev, _canon_row(row), signed, key)
            except (ValueError, TypeError):
                expected = None
            bad = expected is None or not hmac.compare_digest(expected, row["hash"])
        if bad:
            return {"ok": False, "checked": checked, "first_bad_id": row["id"],
                    "signed": False}
        all_signed = all_signed and signed
        prev = row["hash"]
    return {"ok": True, "checked": checked, "first_bad_id": None,
            "signed": bool(checked) and all_signed}


def list_decisions(*, key_id: int | None = None, target: str | None = None,
                   decision: str | None = None, since: int | None = None,
                   limit: int = 50, cursor: int | None = None) -> dict:
    """Newest first. `cursor` is the id to continue below."""
    sql, args = ["SELECT * FROM decisions WHERE 1=1"], []
    for col, val in (("key_id", key_id), ("target", target), ("decision", decision)):
        if val is not None:
            sql.append(f"AND {col} = ?")
            args.append(val)
    if since is not None:
        sql.append("AND ts >= ?")
        args.append(since)
    if cursor is not None:
        sql.append("AND id < ?")
        args.append(cursor)
    limit = max(1, min(int(limit), 500))
    sql.append("ORDER BY id DESC LIMIT ?")
    args.append(limit + 1)
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute(" ".join(sql), args).fetchall()]
    more = len(rows) > limit
    rows = rows[:limit]
    for r in rows:
        r["grant_chain"] = json.loads(r["grant_chain"] or "[]")
        r["enforced_where"] = json.loads(r["enforced_where"] or "{}")
    return {"items": rows, "next_cursor": rows[-1]["id"] if more and rows else None}
