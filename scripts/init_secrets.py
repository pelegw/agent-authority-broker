#!/usr/bin/env python3
"""Generate every broker-owned secret into a .env file (stdlib only).

Run once per deployment, before the first `docker compose up`:

    python scripts/init_secrets.py                  # writes <repo>/.env
    python scripts/init_secrets.py --out /opt/aab/.env
    python scripts/init_secrets.py --rotate SETUP_TOKEN
    python scripts/init_secrets.py --example        # prints the .env.example template

Why a script instead of "openssl rand" instructions: every secret gets the
right shape (the Fernet key must be urlsafe base64 of 32 bytes), the file is
created 0600 from the start, and nothing secret is ever printed, so the
command is safe to run in a recorded terminal or CI log.

Third-party values (GitHub App, Google OAuth, Telegram, Cloudflare Access)
can't be generated; they are written as empty, labelled placeholders and a
checklist of where to obtain each is printed.

`.env.example` in the repo is this file's `--example` output; a test keeps the
two identical so the template can't drift from what the script writes.
"""

from __future__ import annotations

import argparse
import base64
import os
import secrets
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Entry:
    name: str
    comment: str
    # Exactly one of: a generator (broker-owned secret), a checklist hint
    # (third-party placeholder), or neither (plain setting with a default).
    generate: Callable[[], str] | None = None
    obtain: str | None = None
    default: str = ""


def _hex32() -> str:
    return secrets.token_hex(32)


def _fernet_key() -> str:
    # Fernet wants urlsafe base64 of exactly 32 random bytes (with padding).
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")


def _setup_token() -> str:
    return secrets.token_urlsafe(32)


# (section title, entries). Order here is the order in the file.
SECTIONS: list[tuple[str, list[Entry]]] = [
    ("Broker-owned secrets (generated; rotate with --rotate NAME)", [
        Entry("SETUP_TOKEN",
              "One-time token for creating the owner account at /admin. Inert once setup completes.",
              generate=_setup_token),
        Entry("SIDECAR_TOKEN",
              "Shared secret between the broker and the WhatsApp sidecar (internal network only).",
              generate=_hex32),
        Entry("ORIGIN_SECRET",
              "Edge secret a Cloudflare Transform Rule adds as X-AAB-Origin. Used only by the public overlay.",
              generate=_hex32),
        Entry("BROKER_SECRETS_KEY",
              "Fernet key encrypting plugin credentials at rest. Changing it forces plugins to reconnect.",
              generate=_fernet_key),
        Entry("DECISION_SIGNING_KEY",
              "HMAC key for the hash-chained decision record. Keep it stable: old rows verify under it.",
              generate=_hex32),
    ]),
    ("Third-party values (fill in; see the checklist the script prints)", [
        Entry("GITHUB_APP_ID",
              "GitHub App id for the github plugin.",
              obtain="GitHub > Settings > Developer settings > GitHub Apps > New GitHub App; "
                     "the App ID is on the app's General page."),
        Entry("GITHUB_APP_PRIVATE_KEY_PATH",
              "Path (as the broker container sees it) to the GitHub App private key PEM.",
              obtain="The GitHub App's General page > Private keys > Generate a private key."),
        Entry("GOOGLE_OAUTH_CLIENT_ID",
              "Google OAuth client id shared by the gmail, gcal and gdrive plugins.",
              obtain="Google Cloud Console > APIs & Services > Credentials > Create credentials > "
                     "OAuth client ID (Web application)."),
        Entry("GOOGLE_OAUTH_CLIENT_SECRET",
              "Google OAuth client secret for the client id above.",
              obtain="Shown next to the client id in Google Cloud Console > Credentials."),
        Entry("TELEGRAM_BOT_TOKEN",
              "Telegram bot token for approval cards on your phone. Blank = Telegram off.",
              obtain="Message @BotFather on Telegram, send /newbot, copy the token."),
        Entry("CF_ACCESS_TEAM_DOMAIN",
              "Cloudflare Access team domain, e.g. myteam.cloudflareaccess.com (public mode only).",
              obtain="Cloudflare Zero Trust dashboard > Settings > Custom Pages > Team domain."),
        Entry("CF_ACCESS_AUD",
              "Audience (AUD) tag of the Access application protecting /admin (public mode only).",
              obtain="Zero Trust > Access > Applications > your app > Overview > Application Audience (AUD) Tag."),
        Entry("SITE_DOMAIN",
              "Public hostname Cloudflare proxies to this host, e.g. aab.example.com (public mode only).",
              obtain="Your Cloudflare DNS: the proxied (orange-cloud) record pointing at this host."),
    ]),
    ("Settings (defaults are fine for a local run)", [
        Entry("BROKER_PORT", "Port the broker publishes on 127.0.0.1 (local run).", default="8080"),
        Entry("DEVICE_NAME", "Name shown under WhatsApp > Linked devices (applied at pairing).",
              default="AAB"),
        Entry("TZ", "Timezone for logs.", default="UTC"),
        Entry("MCP_ALLOWED_HOSTS", "Host headers the /mcp endpoint accepts (DNS-rebinding guard).",
              default="localhost:*,127.0.0.1:*"),
        Entry("CF_ACCESS_ENABLED",
              "Require a Cloudflare Access identity on the admin plane. Must be true in public mode.",
              default="false"),
        Entry("CF_ACCESS_ALLOWED_EMAILS", "Comma-separated Access identities allowed; blank = any.",
              default=""),
        Entry("ALLOW_INSECURE_ADMIN",
              "Escape hatch: boot in public mode without Cloudflare Access. Leave false.",
              default="false"),
    ]),
]

ENTRIES = {e.name: e for _, entries in SECTIONS for e in entries}
GENERATED = [e.name for e in ENTRIES.values() if e.generate]

HEADER_ENV = [
    "# Agent Authority Broker environment, generated by scripts/init_secrets.py.",
    "# NEVER commit this file. Regenerate one value: python scripts/init_secrets.py --rotate NAME",
]
HEADER_EXAMPLE = [
    "# Agent Authority Broker environment template. Do not fill this in by hand:",
    "# run  python scripts/init_secrets.py  to write a real .env with fresh secrets.",
    "# (This file is that script's --example output; a test keeps them identical.)",
]

# What to do after rotating each generated secret.
ROTATE_HINTS = {
    "SETUP_TOKEN": "Only matters before the owner account exists.",
    "SIDECAR_TOKEN": "Restart both containers: docker compose up -d.",
    "ORIGIN_SECRET": "Update the Cloudflare Transform Rule header value, then restart.",
    "BROKER_SECRETS_KEY": "Stored plugin credentials no longer decrypt: reconnect each plugin.",
    "DECISION_SIGNING_KEY": "Existing decision rows will no longer verify under the new key.",
}


def render(values: dict[str, str], header: list[str]) -> str:
    """The whole file. Every line ends in LF (compose on Linux reads it)."""
    lines = list(header)
    for title, entries in SECTIONS:
        lines += ["", f"# --- {title} ---"]
        for e in entries:
            lines += [f"# {e.comment}", f"{e.name}={values.get(e.name, '')}"]
    return "\n".join(lines) + "\n"


def example_text() -> str:
    values = {e.name: e.default for e in ENTRIES.values()}
    return render(values, HEADER_EXAMPLE)


def _write_private(path: Path, data: bytes) -> None:
    """Atomically write `data` with mode 0600 (best effort on Windows).

    The temp file is created 0600 by mkstemp, so the secret is never briefly
    world-readable; os.replace then swaps it in over any existing file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".env.tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    os.chmod(path, 0o600)
    if os.name == "posix":
        mode = stat.S_IMODE(os.stat(path).st_mode)
        if mode != 0o600:
            sys.exit(f"error: {path} has mode {oct(mode)}, expected 0o600")


def _checklist() -> list[str]:
    out = ["Fill in these third-party values when you enable the feature that needs them:"]
    for e in ENTRIES.values():
        if e.obtain:
            out.append(f"  [ ] {e.name}: {e.obtain}")
    out.append("For a public deploy also set CF_ACCESS_ENABLED=true (see deploy/DEPLOY.md).")
    return out


def cmd_create(path: Path, force: bool) -> int:
    if path.exists() and not force:
        print(f"error: {path} already exists; pass --force to overwrite it "
              f"or --rotate NAME to regenerate one value", file=sys.stderr)
        return 1
    values = {e.name: e.default for e in ENTRIES.values()}
    for name in GENERATED:
        values[name] = ENTRIES[name].generate()
    _write_private(path, render(values, HEADER_ENV).encode("utf-8"))
    print(f"wrote {path} (mode 0600) with {len(GENERATED)} generated secrets:")
    print("  " + ", ".join(GENERATED))
    print()
    print("\n".join(_checklist()))
    return 0


def cmd_rotate(path: Path, name: str) -> int:
    if name not in GENERATED:
        print(f"error: {name} is not a generated secret; choose one of: "
              f"{', '.join(GENERATED)}", file=sys.stderr)
        return 2
    if not path.exists():
        print(f"error: {path} does not exist; run without --rotate first", file=sys.stderr)
        return 1
    data = path.read_bytes()
    # Split keeping line endings so every untouched line stays byte-identical.
    lines = data.splitlines(keepends=True)
    prefix = f"{name}=".encode("ascii")
    hits = [i for i, line in enumerate(lines) if line.startswith(prefix)]
    if len(hits) > 1:
        print(f"error: {name} appears {len(hits)} times in {path}; fix it by hand first",
              file=sys.stderr)
        return 1
    new_value = ENTRIES[name].generate().encode("ascii")
    if hits:
        i = hits[0]
        old = lines[i]
        ending = old[len(old.rstrip(b"\r\n")):]
        lines[i] = prefix + new_value + ending
    else:
        # Older file without this key: append it (with its comment) at the end.
        if lines and not lines[-1].endswith(b"\n"):
            lines[-1] += b"\n"
        lines += [f"# {ENTRIES[name].comment}\n".encode("utf-8"), prefix + new_value + b"\n"]
    _write_private(path, b"".join(lines))
    print(f"rotated {name} in {path}")
    print(f"next: {ROTATE_HINTS[name]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Generate the broker's secrets into a .env file.")
    p.add_argument("--out", type=Path, default=None,
                   help="output path (default: <repo>/.env; with --example: stdout)")
    p.add_argument("--force", action="store_true", help="overwrite an existing file")
    p.add_argument("--rotate", metavar="NAME", help="regenerate one secret in an existing file")
    p.add_argument("--example", action="store_true",
                   help="emit the .env.example template (no secrets)")
    args = p.parse_args(argv)

    if args.example:
        text = example_text()
        if args.out is None:
            sys.stdout.write(text)
        else:
            args.out.write_bytes(text.encode("utf-8"))
        return 0
    path = args.out or (REPO_ROOT / ".env")
    if args.rotate:
        return cmd_rotate(path, args.rotate)
    return cmd_create(path, args.force)


if __name__ == "__main__":
    raise SystemExit(main())
