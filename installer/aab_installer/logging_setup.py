"""One logging setup for every Python service in the stack.

Three byte-identical copies exist, `broker/broker/logging_setup.py`,
`plugin-runtime/aab_plugin_runtime/logging_setup.py` and
`installer/aab_installer/logging_setup.py`: the broker, the plugin runtime
and the installer are separate packages that must not depend on each other,
and a test keeps the files identical so their log lines can never drift.

Logs are the OPERATIONAL trail. The accountability trail stays where it was:
the hash-chained decision record and the audit table. The two meet on the
request id: every log line carries the id of the request (or background job)
it belongs to, and the decision rows that request produced carry the same id.

  configure(service)   one handler on the root logger, writing to stdout
                       (Docker captures it; compose rotates it):
                         2026-09-24T12:00:00.123Z INFO broker.engine [broker 5f0c...] decision ...
                       or one JSON object per line with the same fields
                       (LOG_FORMAT=json). LOG_LEVEL sets the level. uvicorn's
                       server lines go through the same handler; its access
                       log is off (request_log writes ours, query-free). The
                       `aab` CLI logs to stderr: its stdout is its output.
  bind(request_id)     the request id for everything logged inside it; set per
                       HTTP request by request_log.RequestContextMiddleware
                       and per background job by the job itself.
  kv(**fields)         the one way variable data enters a message: key=value
                       pairs that cannot break the line or forge another.
  RedactSecrets        a backstop on the handler. Code never logs a secret in
                       the first place; this replaces anything secret-shaped
                       that slips into a message or a traceback anyway.
"""

from __future__ import annotations

import contextlib
import contextvars
import copy
import json
import logging
import logging.config
import os
import re
import sys
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

# ---- the request context ----------------------------------------------------------

# What an inbound X-Request-Id may look like. Anything else (spaces, quotes,
# newlines, overlong) is ignored and a fresh id generated, so a caller can
# never inject text into a log line or the decision record through it.
REQUEST_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,128}")
NO_ID = "-"


@dataclass
class RequestContext:
    """Mutable on purpose: the ContextVar holds a reference, and the copies
    of the context that thread pools and spawned tasks get all point at the
    same object, so an actor recorded deep inside a request (by the auth
    dependency, in a worker thread) is visible to the access line."""
    request_id: str
    actor: str = NO_ID


class _State:
    service = NO_ID
    configured = False


def _process_wide(name: str, make):
    """One object per PROCESS, not per copy of this file. Both copies load
    into one process when the broker's tests host a plugin app in-process;
    with a context variable (and a configured flag) each, a line logged by
    one copy's code would miss the id the other copy's middleware bound, and
    the second configure() would replace the first one's handler. The stdlib
    logging module is the one namespace both copies share."""
    attr = f"_aab_{name}"
    if not hasattr(logging, attr):
        setattr(logging, attr, make())
    return getattr(logging, attr)


_CURRENT: contextvars.ContextVar = _process_wide(
    "request_context", lambda: contextvars.ContextVar("aab_request_context", default=None))
_state: _State = _process_wide("logging_state", _State)


def new_request_id(prefix: str = "") -> str:
    """A fresh id: 32 hex characters, after `prefix` for background jobs
    (`sched-`, `tg-`) so their lines are recognisable at a glance."""
    return prefix + uuid.uuid4().hex


def valid_request_id(value: object) -> bool:
    return isinstance(value, str) and REQUEST_ID_RE.fullmatch(value) is not None


def current_request_id() -> str | None:
    ctx = _CURRENT.get()
    return ctx.request_id if ctx else None


def current_actor() -> str:
    ctx = _CURRENT.get()
    return ctx.actor if ctx else NO_ID


def set_actor(actor: str) -> None:
    """Record who is acting (`key:<name>`, `owner:<username>`, ...) for the
    access line of the request in progress. A no-op outside a request."""
    ctx = _CURRENT.get()
    if ctx is not None:
        ctx.actor = actor


@contextlib.contextmanager
def bind(request_id: str) -> Iterator[RequestContext]:
    """Run the block under `request_id` (validated; a bad one is replaced)."""
    ctx = RequestContext(request_id if valid_request_id(request_id) else new_request_id())
    token = _CURRENT.set(ctx)
    try:
        yield ctx
    finally:
        _CURRENT.reset(token)


# ---- key=value message data ----------------------------------------------------------

_KV_LIMIT = 200
# logfmt: a value is bare when it has no space, quote, backslash, "=" or
# control character; otherwise it is double-quoted with quotes, backslashes
# and every character that could break a line (C0/C1 controls, U+2028/9)
# escaped. A value can then never end the line or fake a second pair or a
# second log line, whatever an agent put in a key name or a resource id.
_KV_BARE = re.compile(r'[^\s"\\=\x00-\x1f\x7f-\x9f\u2028\u2029]+')
_KV_ESCAPE = re.compile(r'["\\\x00-\x1f\x7f-\x9f\u2028\u2029]')
_KV_ESCAPES = {'"': '\\"', "\\": "\\\\", "\n": "\\n", "\r": "\\r", "\t": "\\t"}


def _escape(match: re.Match) -> str:
    char = match.group()
    return _KV_ESCAPES.get(char) or f"\\u{ord(char):04x}"


def _kv_value(value: object) -> str:
    if value is None or value == "":
        return NO_ID
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple, set, frozenset)):
        items = sorted(value, key=str) if isinstance(value, (set, frozenset)) else value
        text = ",".join(str(v) for v in items) or NO_ID
    else:
        text = str(value)
    if len(text) > _KV_LIMIT:
        text = text[:_KV_LIMIT] + "..."
    if _KV_BARE.fullmatch(text):
        return text
    return '"' + _KV_ESCAPE.sub(_escape, text) + '"'


def kv(**fields: object) -> str:
    """`a=1 b=x,y c=- d="two words"`: the variable part of a log message.
    Lists become comma-joined, None/empty becomes `-`, long values are cut
    at 200 characters, anything unsafe is quoted and escaped (logfmt)."""
    return " ".join(f"{k}={_kv_value(v)}" for k, v in fields.items())


# ---- the redaction backstop --------------------------------------------------------------

REDACTED = "<redacted>"

# Every secret shape the stack knows, in ONE table (tests exercise each row,
# and assert that uuids, hashes, request ids and resource ids survive). The
# 64-hex secrets (plugin tokens, the sidecar token, the signing key) cannot
# be told apart from a sha256, so nothing here matches them: they are kept
# out of logs structurally, never by pattern.
SECRET_PATTERNS: tuple[tuple[str, re.Pattern, str], ...] = (
    ("pem_block", re.compile(r"-----BEGIN [A-Z0-9 ]{1,64}-----.*?"
                             r"(?:-----END [A-Z0-9 ]{1,64}-----|\Z)", re.S), REDACTED),
    # Eight characters and up: a credential, not the word in a sentence.
    ("bearer", re.compile(r"\b(Bearer)\s+[A-Za-z0-9._~+/=-]{8,}", re.I), r"\1 " + REDACTED),
    ("admin_token", re.compile(r"aab_admin_[0-9a-f]{48}"), REDACTED),
    ("agent_key", re.compile(r"aab_[0-9a-f]{48}"), REDACTED),
    ("telegram_bot_token", re.compile(r"\d{5,}:[A-Za-z0-9_-]{30,}"), REDACTED),
    ("google_access_token", re.compile(r"ya29\.[\w-]+"), REDACTED),
    ("google_refresh_token", re.compile(r"\b1//[\w-]{20,}"), REDACTED),
    ("google_client_secret", re.compile(r"GOCSPX-[\w-]{10,}"), REDACTED),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), REDACTED),
    ("github_fine_grained_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), REDACTED),
    # A Fernet key: urlsafe base64 of 32 bytes, exactly 43 chars and one "=".
    ("fernet_key", re.compile(r"(?<![\w-])[A-Za-z0-9_-]{43}=(?![\w=-])"), REDACTED),
    ("session_cookie", re.compile(r"(aab_session=)[^;\s]+"), r"\1" + REDACTED),
    # A header or field dump: the value after a secret-bearing name.
    ("secret_field", re.compile(
        r"(?i)\b((?:x-plugin-token|x-installer-token|x-internal-token|x-aab-origin|password|"
        r"passwd|client_secret|refresh_token|access_token|id_token|setup_token|private_key_pem|"
        r"private_key|api_key|secret|pat)['\"]?\s*[:=]\s*['\"]?)[^\s'\",;}&]+"),
     r"\1" + REDACTED),
)


def redact(text: str) -> str:
    for _, pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


_TRACEBACKS = logging.Formatter()


class RedactSecrets(logging.Filter):
    """Handler filter: returns a redacted COPY of the record (Python 3.12
    filters may), so other handlers (a test's capture handler) still see
    exactly what the code logged and can prove nothing secret was logged."""

    def filter(self, record: logging.LogRecord) -> logging.LogRecord:
        try:
            message = record.getMessage()
        except Exception:                      # a malformed record: log its template
            message = str(record.msg)
        exc_text = record.exc_text
        if record.exc_info and not exc_text:
            exc_text = _TRACEBACKS.formatException(record.exc_info)
        out = copy.copy(record)
        out.msg, out.args = redact(message), None
        out.exc_info, out.exc_text = None, redact(exc_text) if exc_text else None
        out.stack_info = redact(record.stack_info) if record.stack_info else None
        return out


class RequestIdFilter(logging.Filter):
    """Handler filter: stamps the request id (`-` outside any) and the
    service name on the record, for the formatters."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = current_request_id() or NO_ID
        if not hasattr(record, "service"):
            record.service = _state.service
        return True


class StripQueryString(logging.Filter):
    """On `uvicorn.access`, belt and braces: configure() leaves that logger
    no handler path, so uvicorn writes no access line at all (request_log
    writes ours). Should something attach a handler anyway, uvicorn puts the
    path WITH its query string in args[2], and the OAuth callback's query
    carries an authorization code: this cuts it off."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            record.args = (*args[:2], args[2].split("?", 1)[0], *args[3:])
        return True


# ---- formatters and the handler -------------------------------------------------------------

TEXT_FORMAT = "%(asctime)sZ %(levelname)s %(name)s [%(service)s %(request_id)s] %(message)s"


def _utc(record: logging.LogRecord) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + \
        f".{int(record.msecs):03d}"


def _stamp(record: logging.LogRecord) -> None:
    # The filters normally do this; a formatter used without them must not fail.
    record.__dict__.setdefault("request_id", current_request_id() or NO_ID)
    record.__dict__.setdefault("service", _state.service)


class TextFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__(TEXT_FORMAT)

    def formatTime(self, record, datefmt=None) -> str:
        return _utc(record)

    def format(self, record: logging.LogRecord) -> str:
        _stamp(record)
        return super().format(record)


class JsonFormatter(logging.Formatter):
    """One JSON object per line (ASCII-escaped, so a message can never
    contain a raw newline), with the text format's fields."""

    def format(self, record: logging.LogRecord) -> str:
        _stamp(record)
        entry = {"ts": _utc(record) + "Z", "level": record.levelname, "logger": record.name,
                 "service": record.service, "request_id": record.request_id,
                 "message": record.getMessage()}
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            entry["exc"] = record.exc_text
        if record.stack_info:
            entry["stack"] = self.formatStack(record.stack_info)
        return json.dumps(entry, ensure_ascii=True)


class ConsoleHandler(logging.StreamHandler):
    """A stream handler on whatever `sys.stdout` (or `sys.stderr`) is when a
    record is emitted, not when the handler was built: a test's output
    capture, or a host that swaps the stream, still receives every line.
    Services log to stdout; the `aab` CLI to stderr (its stdout is its
    output)."""

    def __init__(self, stream_name: str = "stdout") -> None:
        if stream_name not in ("stdout", "stderr"):
            raise ValueError("stream_name must be stdout or stderr")
        self.stream_name = stream_name
        super().__init__()

    @property
    def stream(self):
        return getattr(sys, self.stream_name)

    @stream.setter
    def stream(self, _value) -> None:
        pass


# ---- configure ------------------------------------------------------------------------------

LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
FORMATS = ("text", "json")
UVICORN = ("uvicorn", "uvicorn.error", "uvicorn.access")
# Held at WARNING whatever LOG_LEVEL says: at INFO/DEBUG these log request
# URLs with their query strings (httpx: a Gmail search, the bot token in the
# Telegram path), protocol payloads (mcp: tool arguments) or form fields.
QUIET = ("httpx", "httpcore", "mcp", "multipart", "python_multipart", "hpack", "urllib3")


def _choice(raw: str | None, allowed: tuple[str, ...], default: str) -> tuple[str, bool]:
    """(value, was_invalid). Case-insensitive; WARN means WARNING."""
    value = (raw or "").strip()
    if not value:
        return default, False
    for option in allowed:
        if value.lower() == option.lower() or (option == "WARNING" and value.upper() == "WARN"):
            return option, False
    return default, True


def configure(service: str, environ: dict | None = None, *, force: bool = False,
              stream: str = "stdout") -> bool:
    """Set up logging for this process as `service`. Returns False (and
    changes nothing) when logging is already configured: one process is one
    service, and a second call (a plugin app built inside the broker's test
    process, or the app imported by the `aab` CLI) must not re-plumb
    handlers under code that is running. `force` exists for tests of this
    function; `stream` is stdout for services, stderr for the CLI."""
    if _state.configured and not force:
        return False
    env = os.environ if environ is None else environ
    level, bad_level = _choice(env.get("LOG_LEVEL"), LEVELS, "INFO")
    fmt, bad_format = _choice(env.get("LOG_FORMAT"), FORMATS, "text")
    _state.service = service
    # dictConfig adds a logger's filters without removing earlier ones (by
    # name: the other copy of this file has its own class).
    for name in UVICORN:
        for f in list(logging.getLogger(name).filters):
            if type(f).__name__ == StripQueryString.__name__:
                logging.getLogger(name).removeFilter(f)
    uvicorn = {"level": level, "handlers": [], "propagate": True}
    logging.config.dictConfig({
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {"context": {"()": RequestIdFilter}, "redact": {"()": RedactSecrets},
                    "strip_query": {"()": StripQueryString}},
        "formatters": {"text": {"()": TextFormatter}, "json": {"()": JsonFormatter}},
        "handlers": {"console": {"()": ConsoleHandler, "stream_name": stream, "formatter": fmt,
                                 "filters": ["context", "redact"]}},
        # uvicorn installs its own handlers (propagate off) before the app is
        # imported; configuring its loggers here drops them, so its lines go
        # through the one handler above instead of printing twice.
        # uvicorn.access gets NO handler path: uvicorn writes an access line
        # only when that logger has one (hasHandlers(); --no-access-log is
        # exactly "none"), and its line carries the query string. With none,
        # uvicorn never writes one, with or without the flag; request_log
        # writes ours.
        "loggers": {"uvicorn": uvicorn, "uvicorn.error": uvicorn,
                    "uvicorn.access": {"level": "WARNING", "handlers": [], "propagate": False,
                                       "filters": ["strip_query"]},
                    **{name: {"level": "WARNING"} for name in QUIET}},
        "root": {"level": level, "handlers": ["console"]},
    })
    _state.configured = True
    log = logging.getLogger(__name__)
    if bad_level:
        log.warning("LOG_LEVEL not recognised; using INFO %s", kv(allowed=LEVELS))
    if bad_format:
        log.warning("LOG_FORMAT not recognised; using text %s", kv(allowed=FORMATS))
    log.info("logging configured %s", kv(service=service, level=level, format=fmt))
    return True
