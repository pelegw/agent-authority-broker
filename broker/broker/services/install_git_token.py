"""The installer's GitHub token: a write-only console setting, kept encrypted by the broker.

Cloning a private plugin repository needs a read-only GitHub token. It is a
third-party credential, so it is handled exactly like the Telegram bot token
(notify/telegram.py): the owner enters it in the console (Plugins, + Add
plugin), the broker stores it encrypted through crypto.py under
BROKER_SECRETS_KEY (slot `broker`, name `installer_git_token`), and no route
ever returns it. It is not in .env, and the installer never stores it: the
broker puts it in the body of the one inspect, install or upgrade request
that needs it (services/plugin_install.py), and the installer hands it to
that request's or that job's git network commands through GIT_ASKPASS, for
github.com sources on its allowlist only, then forgets it.

  state()             unset | set | unreadable (stored, but not under the current key)
  set_token(ctx, t)   check the shape loosely, store it encrypted, audit installer.git_token.set
  clear_token(ctx)    delete it (no key needed), audit installer.git_token.clear
  request_fields()    {"git_token": value} when set, {} when unset; 409 when unreadable

Why an unreadable token is a 409 and not a quiet anonymous clone: the owner
stored a token because the repository is private, so cloning without it can
only fail later with a vaguer error. Saying "re-enter it" is the useful answer.

Logged and audited by name only: never the value, its length or any part of it.
"""

from __future__ import annotations

import logging
import re

from .. import crypto
from ..audit import audit
from ..errors import PolicyError
from ..logging_setup import kv

log = logging.getLogger(__name__)

TOKEN_NAME = "installer_git_token"
# Loose on purpose (fine-grained, classic and future GitHub token formats all
# fit): printable ASCII with no whitespace, so it is one line for the askpass
# script, and a length no real token falls outside. The installer applies the
# same rule (installer/aab_installer/git.py, GIT_TOKEN_RE). Always used with
# fullmatch: `$` would let a trailing newline through.
TOKEN_RE = re.compile(r"[\x21-\x7e]{20,255}")

UNREADABLE_MESSAGE = ("the installer's GitHub token can no longer be decrypted "
                      "(BROKER_SECRETS_KEY changed): enter it again under Plugins, "
                      "+ Add plugin, or clear it there")


def state() -> str:
    """unset | set | unreadable (stored, but not decryptable under the current key)."""
    return crypto.state(crypto.BROKER_SLOT, TOKEN_NAME)


def view() -> dict:
    """What the console shows around the token field. Never the token."""
    return {"git_token": state(), "secrets_key_configured": crypto.key_configured()}


def _audit(ctx, action: str, detail: dict) -> None:
    audit(ctx.username, action, "installer", detail, "ok",
          actor_principal=ctx.principal_id, actor_via=ctx.via)


def set_token(ctx, token: str) -> dict:
    """Store the token (write-only). A paste's surrounding whitespace is
    dropped; whitespace inside, or any other shape, is refused unseen."""
    token = (token or "").strip()
    if not TOKEN_RE.fullmatch(token):
        raise PolicyError(400, "that is not a GitHub token (expected 20 to 255 printable "
                               "characters with no spaces)", "invalid_token")
    if not crypto.key_configured():
        raise PolicyError(409, "BROKER_SECRETS_KEY is not set, so the token cannot be stored; "
                               "run scripts/init_secrets.py and restart", "secrets_key_missing")
    replaced = state() != "unset"
    crypto.put(crypto.BROKER_SLOT, TOKEN_NAME, token)
    _audit(ctx, "installer.git_token.set", {"replaced": replaced})
    log.info("installer github token stored %s", kv(replaced=replaced, by=ctx.username,
                                                    via=ctx.via))
    return view()


def clear_token(ctx) -> dict:
    """Delete the stored token. Works without the key, so an unreadable one
    can always be removed."""
    removed = crypto.delete(crypto.BROKER_SLOT, TOKEN_NAME)
    _audit(ctx, "installer.git_token.clear", {"removed": removed})
    log.info("installer github token cleared %s", kv(removed=removed, by=ctx.username,
                                                     via=ctx.via))
    return view()


def request_fields() -> dict:
    """The fields an installer request carries for the token: the plaintext
    when one is stored, nothing when none is. Fails closed (409) on a token
    that is stored but cannot be read, or reads back malformed."""
    try:
        token = crypto.get(crypto.BROKER_SLOT, TOKEN_NAME)
    except (crypto.SecretsUnavailable, crypto.SecretsUnreadable):
        raise PolicyError(409, UNREADABLE_MESSAGE, "git_token_unreadable") from None
    if token is None:
        return {}
    if not TOKEN_RE.fullmatch(token):
        raise PolicyError(409, UNREADABLE_MESSAGE, "git_token_unreadable")
    return {"git_token": token}
