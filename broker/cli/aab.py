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
  aab tokens list
  aab tokens revoke <token-id>
  aab sessions list
  aab sessions revoke <session-id>
  aab password                                # prompts for current and new

Passwords are only ever read with getpass, never from arguments (which end up
in shell history and process listings).
"""

import argparse
import getpass
import json
import os
import sys

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
    tk.add_parser("list")
    tr = tk.add_parser("revoke")
    tr.add_argument("id")

    ss = sub.add_parser("sessions", help="manage console login sessions").add_subparsers(
        dest="sub", required=True)
    ss.add_parser("list")
    sr = ss.add_parser("revoke")
    sr.add_argument("id")

    sub.add_parser("password", help="change the owner password")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

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

    if not args.token:
        print("set AAB_ADMIN_TOKEN in the environment or pass --token", file=sys.stderr)
        return 2
    with make_client(args.url, args.token) as c:
        if args.cmd == "tokens" and args.sub == "create":
            r = c.post("/v1/admin/tokens", json={"name": args.name,
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
        else:  # unreachable: argparse enforces the choices
            return 2
    return _finish(r)


def _finish(r: httpx.Response) -> int:
    if r.status_code >= 400:
        print(_error(r), file=sys.stderr)
        return 1
    show(r.json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
