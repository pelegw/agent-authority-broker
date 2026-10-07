#!/usr/bin/env python3
"""aab: admin CLI for the Agent Authority Broker.

Talks to the same admin REST API as the console, over httpx. Configuration:
  --url   / AAB_URL          broker base URL (default http://127.0.0.1:8080)
  --token / AAB_ADMIN_TOKEN  an aab_admin_ token (mint the first one in the
                             console; later ones with `aab tokens create`)
  CF_ACCESS_CLIENT_ID / CF_ACCESS_CLIENT_SECRET  Cloudflare Access service
                             token, when the admin plane is behind Access

Examples:
  aab setup --username owner                  # SETUP_TOKEN from env or --setup-token
  aab tokens create --name deploy --expires-in-hours 24
  aab tokens create --name uptimerobot --scope monitor   # /health only
  aab tokens list
  aab tokens revoke <token-id>
  aab sessions list
  aab sessions revoke <session-id>
  aab password                                # prompts for current and new
  aab skill build --all-plugins --base-url "{{BASE_URL}}" --out SKILL.md   # offline

  aab keys create --name bot --capabilities '<JSON list>' [--role read-draft]
      (capabilities e.g. [{"target": "whatsapp", "actions": ["list_chats"]}];
       --role is a ceiling that only caps them, default full)
  aab keys list | rotate <id> | disable <id>
  aab grants list [--status pending] | approve|reject|revoke <grant-id>
  aab actions list [--status pending] | approve|reject|cancel <action-id>
  aab plugins list | enable|disable|health <id>
  aab plugins config <id> --set greeting=hi --secret api_secret   # secret read with getpass
  aab hidden list [--target t] | add <target> <kind> <id> [--label L --reason R]
  aab hidden rm <target> <kind> <id>
  aab decisions list [--key 3 --target t --decision deny --limit 50] | verify
  aab simulate [--hours 8] [--seed 7] [--json]   # approval-volume simulation, local only
  aab audit export --state cursor.json [--db broker.db] [--hash-resources] [--loop] [--reset]
      (local, read-only: prints new decisions and audit_log rows as JSON lines;
       the settings AUDIT_EXPORT_STATE, BROKER_DB, AUDIT_EXPORT_HASH_RESOURCES,
       AUDIT_EXPORT_INTERVAL give the defaults; docs/logging.md)

Passwords and plugin secrets are only ever read with getpass, never from
arguments (which end up in shell history and process listings).
"""

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

import httpx

from broker import __version__

DEFAULT_URL = "http://127.0.0.1:8080"


def make_client(url: str, token: str | None) -> httpx.Client:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    # When the admin plane is behind Cloudflare Access, a service token lets the
    # CLI through non-interactively: Cloudflare validates these and injects the
    # identity JWT the broker verifies. Harmless when Access is not in use.
    cf_id = os.environ.get("CF_ACCESS_CLIENT_ID")
    cf_secret = os.environ.get("CF_ACCESS_CLIENT_SECRET")
    if cf_id and cf_secret:
        headers["CF-Access-Client-Id"] = cf_id
        headers["CF-Access-Client-Secret"] = cf_secret
    return httpx.Client(base_url=url, headers=headers, timeout=30)


def show(data) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def _error(resp: httpx.Response) -> str:
    try:
        return f"error {resp.status_code}: {resp.json().get('error', resp.text)}"
    except ValueError:
        return f"error {resp.status_code}: {resp.text}"


def _new_password(prompt: str = "New password: ") -> str | None:
    first = getpass.getpass(prompt)
    if first != getpass.getpass("Repeat: "):
        print("passwords do not match", file=sys.stderr)
        return None
    return first


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="aab", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"aab {__version__}")
    p.add_argument("--url", default=os.environ.get("AAB_URL", DEFAULT_URL))
    p.add_argument("--token", default=os.environ.get("AAB_ADMIN_TOKEN"))
    sub = p.add_subparsers(dest="cmd", required=True)

    st = sub.add_parser("setup", help="create the owner account (once)")
    st.add_argument("--username", required=True)
    st.add_argument("--setup-token", default=os.environ.get("SETUP_TOKEN"))

    tk = sub.add_parser("tokens", help="manage admin tokens").add_subparsers(
        dest="sub", required=True)
    tc = tk.add_parser("create")
    tc.add_argument("--name", required=True)
    tc.add_argument("--expires-in-hours", type=int, default=None)
    tc.add_argument("--scope", choices=["admin", "monitor"], default="admin",
                    help="monitor: accepted by /health and /v1/health only")
    tk.add_parser("list")
    tr = tk.add_parser("revoke")
    tr.add_argument("id")

    ss = sub.add_parser("sessions", help="manage console login sessions").add_subparsers(
        dest="sub", required=True)
    ss.add_parser("list")
    sr = ss.add_parser("revoke")
    sr.add_argument("id")

    sub.add_parser("password", help="change the owner password")
    _skill_commands(sub)
    _engine_commands(sub)
    _audit_commands(sub)

    sm = sub.add_parser("simulate", help="approval-volume simulation (local; needs a "
                                         "source checkout)")
    sm.add_argument("--hours", type=int, default=8)
    sm.add_argument("--seed", type=int, default=7)
    sm.add_argument("--json", action="store_true", help="print only the JSON summary")
    return p


def _json_arg(text: str):
    try:
        return json.loads(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not valid JSON: {exc}") from exc


def _engine_commands(sub) -> None:
    """Phase 3: keys, grants, actions, plugins, hidden resources, decisions."""
    ky = sub.add_parser("keys", help="manage agent keys").add_subparsers(dest="sub", required=True)
    kc = ky.add_parser("create")
    kc.add_argument("--name", required=True)
    kc.add_argument("--role", default=None,
                    help="the key's ceiling: read-only | read-draft | read-act | full. It "
                         "never grants, only caps the capabilities; omitted, the broker's "
                         "default (full) applies and the capabilities decide")
    kc.add_argument("--rate", type=int, default=6)
    kc.add_argument("--expires-at", type=int, default=None)
    kc.add_argument("--capabilities", type=_json_arg, default=[],
                    help="JSON list of capability objects")
    kc.add_argument("--denies", type=_json_arg, default=None,
                    help='JSON {target: {kind: [ids]}}')
    ky.add_parser("list")
    for verb in ("rotate", "disable"):
        ky.add_parser(verb).add_argument("id", type=int)

    gr = sub.add_parser("grants", help="list and decide grants").add_subparsers(
        dest="sub", required=True)
    gr.add_parser("list").add_argument("--status", default=None)
    for verb in ("approve", "reject", "revoke"):
        gr.add_parser(verb).add_argument("id")

    ac = sub.add_parser("actions", help="queued actions").add_subparsers(
        dest="sub", required=True)
    ac.add_parser("list").add_argument("--status", default=None)
    for verb in ("approve", "reject", "cancel"):
        ac.add_parser(verb).add_argument("id")

    pl = sub.add_parser("plugins", help="plugins").add_subparsers(dest="sub", required=True)
    pl.add_parser("list")
    for verb in ("enable", "disable", "health"):
        pl.add_parser(verb).add_argument("id")
    pc = pl.add_parser("config")
    pc.add_argument("id")
    pc.add_argument("--set", action="append", default=[], metavar="NAME=VALUE",
                    help="non-secret field; VALUE is parsed as JSON when it can be")
    pc.add_argument("--secret", action="append", default=[], metavar="NAME",
                    help="secret field, prompted for with getpass")

    hd = sub.add_parser("hidden", help="hidden resources").add_subparsers(
        dest="sub", required=True)
    hd.add_parser("list").add_argument("--target", default=None)
    ha = hd.add_parser("add")
    hr = hd.add_parser("rm")
    for p in (ha, hr):
        p.add_argument("target")
        p.add_argument("kind")
        p.add_argument("resource_id")
    ha.add_argument("--label", default="")
    ha.add_argument("--reason", default="")

    dc = sub.add_parser("decisions", help="the decision record").add_subparsers(
        dest="sub", required=True)
    dl = dc.add_parser("list")
    dl.add_argument("--key", type=int, default=None)
    dl.add_argument("--target", default=None)
    dl.add_argument("--decision", default=None)
    dl.add_argument("--limit", type=int, default=50)
    dc.add_parser("verify")


def _config_body(args) -> dict:
    config = {}
    for item in args.set:
        name, sep, value = item.partition("=")
        if not sep:
            raise SystemExit(f"--set expects NAME=VALUE, got {item!r}")
        try:
            config[name] = json.loads(value)
        except ValueError:
            config[name] = value
    for name in args.secret:
        config[name] = getpass.getpass(f"{name}: ")
    return {"config": config}


def _engine_request(c: httpx.Client, args) -> httpx.Response | None:
    """The phase 3 commands; None when args are not one of them."""
    cmd, sub = args.cmd, getattr(args, "sub", None)
    if cmd == "keys":
        if sub == "create":
            body = {"name": args.name, "rate_per_min": args.rate,
                    "expires_at": args.expires_at, "capabilities": args.capabilities,
                    "denies": args.denies}
            # No --role: send none, so the broker's owner default is the one
            # source of truth rather than a second copy here.
            if args.role is not None:
                body["role"] = args.role
            return c.post("/v1/admin/keys", json=body)
        if sub == "list":
            return c.get("/v1/admin/keys")
        if sub == "rotate":
            return c.post(f"/v1/admin/keys/{args.id}/rotate")
        if sub == "disable":
            return c.patch(f"/v1/admin/keys/{args.id}", json={"disabled": True})
    if cmd == "grants":
        if sub == "list":
            return c.get("/v1/admin/grants", params={"status": args.status} if args.status else {})
        return c.post(f"/v1/admin/grants/{args.id}/{sub}")
    if cmd == "actions":
        if sub == "list":
            return c.get("/v1/admin/actions", params={"status": args.status} if args.status else {})
        return c.post(f"/v1/admin/actions/{args.id}/{sub}")
    if cmd == "plugins":
        if sub == "list":
            return c.get("/v1/admin/plugins")
        if sub == "config":
            return c.patch(f"/v1/admin/plugins/{args.id}", json=_config_body(args))
        return c.post(f"/v1/admin/plugins/{args.id}/{sub}")
    if cmd == "hidden":
        if sub == "list":
            return c.get("/v1/admin/hidden", params={"target": args.target} if args.target else {})
        if sub == "add":
            return c.post("/v1/admin/hidden", json={
                "target": args.target, "kind": args.kind, "resource_id": args.resource_id,
                "label": args.label, "reason": args.reason})
        return c.delete(f"/v1/admin/hidden/{args.target}/{args.kind}/{args.resource_id}")
    if cmd == "decisions":
        if sub == "verify":
            return c.get("/v1/admin/decisions/verify")
        params = {k: v for k, v in (("key", args.key), ("target", args.target),
                                    ("decision", args.decision), ("limit", args.limit))
                  if v is not None}
        return c.get("/v1/admin/decisions", params=params)
    return None


def _log_to_stderr(service: str = "aab-cli") -> None:
    """The CLI's stdout is its output (JSON, the skill doc, audit rows).
    `simulate`, `skill` and `audit` run broker code in this process, so
    logging is set up here first: to stderr, at WARNING unless LOG_LEVEL says
    otherwise. The app's own configure() at import is then a no-op (one
    process, one setup)."""
    from broker import logging_setup
    env = dict(os.environ)
    env.setdefault("LOG_LEVEL", "WARNING")
    logging_setup.configure(service, env, stream="stderr")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # The audit exporter runs as its own service, and its lines say so.
    _log_to_stderr("audit-exporter" if args.cmd == "audit" else "aab-cli")
    if args.cmd == "skill":
        return _skill(args)
    if args.cmd == "audit":
        return _audit(args)

    if args.cmd == "setup":
        if not args.setup_token:
            print("set SETUP_TOKEN in the environment or pass --setup-token", file=sys.stderr)
            return 2
        password = _new_password("Owner password (12+ characters): ")
        if password is None:
            return 1
        with make_client(args.url, None) as c:
            r = c.post("/auth/setup", json={"setup_token": args.setup_token,
                                            "username": args.username,
                                            "password": password})
        return _finish(r)

    if args.cmd == "simulate":
        return _simulate(args)

    if not args.token:
        print("set AAB_ADMIN_TOKEN in the environment or pass --token", file=sys.stderr)
        return 2
    with make_client(args.url, args.token) as c:
        if args.cmd == "tokens" and args.sub == "create":
            r = c.post("/v1/admin/tokens", json={"name": args.name, "scope": args.scope,
                                                 "expires_in_hours": args.expires_in_hours})
        elif args.cmd == "tokens" and args.sub == "list":
            r = c.get("/v1/admin/tokens")
        elif args.cmd == "tokens" and args.sub == "revoke":
            r = c.post(f"/v1/admin/tokens/{args.id}/revoke")
        elif args.cmd == "sessions" and args.sub == "list":
            r = c.get("/v1/admin/sessions")
        elif args.cmd == "sessions" and args.sub == "revoke":
            r = c.post(f"/v1/admin/sessions/{args.id}/revoke")
        elif args.cmd == "password":
            current = getpass.getpass("Current password: ")
            new = _new_password()
            if new is None:
                return 1
            r = c.post("/v1/admin/password", json={"current_password": current,
                                                   "new_password": new})
        else:
            r = _engine_request(c, args)
            if r is None:     # unreachable: argparse enforces the choices
                return 2
    return _finish(r)


def _simulate(args) -> int:
    """Run tests/simulation in this process against a throwaway database.
    It talks to no broker and needs no token. The simulation lives with the
    tests (it drives the in-process echo plugin), which are not part of the
    installed package, so this works from a source checkout only."""
    checkout = Path(__file__).resolve().parents[1]      # broker/
    if (checkout / "tests" / "simulation").is_dir():
        # First on the path, not merely present: the editable plugin-runtime
        # install puts its own top-level `tests` package on sys.path too.
        if str(checkout) in sys.path:
            sys.path.remove(str(checkout))
        sys.path.insert(0, str(checkout))
    try:
        from tests.simulation import simulate
    except ImportError as exc:
        print(f"aab simulate needs a source checkout with broker/tests ({exc})",
              file=sys.stderr)
        return 2
    argv = ["--hours", str(args.hours), "--seed", str(args.seed)]
    if args.json:
        argv.append("--json")
    return simulate.main(argv)


def _finish(r: httpx.Response) -> int:
    if r.status_code >= 400:
        print(_error(r), file=sys.stderr)
        return 1
    show(r.json())
    return 0


# ---- aab skill (phase 5): render the agent skill doc, offline ----------------------

def _skill_commands(sub) -> None:
    sk = sub.add_parser("skill", help="the generated agent skill doc").add_subparsers(
        dest="sub", required=True)
    b = sk.add_parser("build", help="render SKILL.md from the vendored manifests (no broker "
                                    "or token needed)")
    which = b.add_mutually_exclusive_group(required=True)
    which.add_argument("--all-plugins", action="store_true",
                       help="every vendored manifest (what CI checks)")
    which.add_argument("--plugin", action="append", metavar="ID",
                       help="only this plugin (repeatable)")
    b.add_argument("--base-url", default="{{BASE_URL}}",
                   help='written into the doc; default the literal "{{BASE_URL}}"')
    b.add_argument("--out", default=None, help="file to write (default: stdout)")


def _skill(args) -> int:
    """Render from broker/broker/targets/*/manifest.yaml, never from a
    database: the committed integrations/ file must not depend on which
    plugins some deployment happens to have enabled."""
    from pathlib import Path

    from broker.skill.generator import skill_file, vendored_manifests

    manifests = vendored_manifests()
    if not args.all_plugins:
        missing = sorted(set(args.plugin) - set(manifests))
        if missing:
            print(f"no vendored manifest for: {', '.join(missing)}", file=sys.stderr)
            return 2
        manifests = {pid: m for pid, m in manifests.items() if pid in args.plugin}
    text = skill_file(manifests, args.base_url)
    if args.out is None:
        sys.stdout.write(text)
        return 0
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    # LF on every platform: the file is committed and diffed byte for byte.
    with out.open("w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    print(f"wrote {out} ({len(text.encode('utf-8'))} bytes)")
    return 0


# ---- aab audit export: the audit record as JSON lines, offline ---------------------

def _audit_commands(sub) -> None:
    au = sub.add_parser("audit", help="the audit record, for a log shipper").add_subparsers(
        dest="sub", required=True)
    ex = au.add_parser("export", help="print new decisions and audit_log rows as JSON lines "
                                      "(reads broker.db read-only; no broker or token needed)")
    ex.add_argument("--db", default=None, help="broker.db (default: BROKER_DB)")
    ex.add_argument("--state", default=None,
                    help="the cursor file (default: AUDIT_EXPORT_STATE)")
    ex.add_argument("--hash-resources", action="store_true", default=None,
                    help="replace resource ids with sha256:<16 hex> (default: "
                         "AUDIT_EXPORT_HASH_RESOURCES)")
    ex.add_argument("--loop", action="store_true",
                    help="run again every --interval seconds until stopped")
    ex.add_argument("--interval", type=int, default=None,
                    help="seconds between runs with --loop (default: AUDIT_EXPORT_INTERVAL, "
                         "3600 when unset)")
    ex.add_argument("--reset", action="store_true",
                    help="ignore the cursor and export everything again (with --loop: "
                         "until a run succeeds)")


def _audit_settings():
    """The broker settings, or None after saying on stderr which variable
    could not be read. Names and pydantic's reason only, never the value:
    the same validation covers the broker's secrets."""
    from pydantic import ValidationError

    from broker.config import get_settings
    try:
        return get_settings()
    except ValidationError as exc:
        for err in exc.errors():
            name = "_".join(str(part) for part in err["loc"]).upper()
            print(f"{name}: {err['msg']}", file=sys.stderr)
        # A privacy switch nobody can read must not run as "off".
        print("aab audit export refuses to run with a setting it cannot read "
              "(docs/configuration.md)", file=sys.stderr)
        return None


def _audit(args) -> int:
    """Read broker.db directly (read-only), never the API: the exporter runs
    beside the broker with the volume mounted read-only, and needs neither
    a token nor a network. Rows go to stdout, its own lines to stderr."""
    import signal

    from broker import audit_export

    settings = _audit_settings()
    if settings is None:
        return 2
    state = args.state or settings.audit_export_state
    if not state:
        print("set AUDIT_EXPORT_STATE or pass --state (the cursor file)", file=sys.stderr)
        return 2
    hash_resources = args.hash_resources or settings.audit_export_hash_resources
    interval = settings.audit_export_interval if args.interval is None else args.interval
    if args.loop and interval < 1:
        print("--interval must be a whole number of seconds, 1 or more", file=sys.stderr)
        return 2
    db_path = args.db or settings.broker_db
    # --reset stays armed until a run gets through: a first run that fails
    # (the broker not up yet) must not quietly turn it into a plain export.
    reset = args.reset

    def run() -> bool:
        nonlocal reset
        ok = audit_export.run_once(db_path, state, sys.stdout,
                                   hash_resources=hash_resources, reset=reset)
        if ok:
            reset = False
        return ok

    if not args.loop:
        return 0 if run() else 1
    stop = audit_export.StopFlag()
    # docker stop sends SIGTERM to PID 1, which has no default handler: without
    # this the container would wait out the stop timeout and be killed. The
    # handler only sets a flag (StopFlag says why not threading.Event).
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, stop.set)
    audit_export.run_loop(run, interval, stop)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
