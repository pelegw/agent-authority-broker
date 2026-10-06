"""logging_setup: one stdout handler, the line format, LOG_LEVEL / LOG_FORMAT,
uvicorn's loggers folded into the one handler, the context and redaction
filters, and kv(). The three copies (broker, plugin runtime, installer) must
be byte-identical, and so must the three request_log.py copies."""

import io
import json
import logging
import logging.config
import re
import sys
from pathlib import Path

import pytest

from broker import logging_setup as ls

REPO = Path(__file__).resolve().parents[2]
LINE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z) (\w+) (\S+) \[(\S+) (\S+)\] (.*)$")


@pytest.mark.parametrize("name", ["logging_setup.py", "request_log.py"])
@pytest.mark.parametrize("copy", ["plugin-runtime/aab_plugin_runtime",
                                  "installer/aab_installer"])
def test_every_copy_is_identical_to_the_brokers(name, copy):
    broker = (REPO / "broker" / "broker" / name).read_bytes()
    other = (REPO / copy / name).read_bytes()
    assert broker == other, f"{copy}/{name} differs: copy the broker's over it"


def test_both_copies_share_one_context_and_one_setup_per_process():
    """The broker's tests host plugin apps in this process: the runtime's
    middleware binds ids through its copy, the broker's code logs through
    the other, and the first configure() must stay the only one."""
    from aab_plugin_runtime import logging_setup as runtime_copy
    assert runtime_copy is not ls
    assert runtime_copy._CURRENT is ls._CURRENT and runtime_copy._state is ls._state
    with runtime_copy.bind("bound-by-the-runtime"):
        assert ls.current_request_id() == "bound-by-the-runtime"
    assert runtime_copy.configure("plugin-x") is False
    assert ls._state.service == "broker"


@pytest.fixture()
def fresh():
    """configure(force=True) under a test, then put every touched logger and
    the module state back as they were."""
    names = ["", *ls.UVICORN, *ls.QUIET, "t"]
    saved = {n: (lg.level, lg.handlers[:], lg.propagate, lg.filters[:])
             for n in names for lg in [logging.getLogger(n)]}
    state = (ls._state.service, ls._state.configured)

    def configure(**env):
        assert ls.configure("svc", environ=env, force=True) is True

    yield configure
    for n, (level, handlers, propagate, filters) in saved.items():
        lg = logging.getLogger(n)
        lg.setLevel(level)
        lg.handlers[:] = handlers
        lg.propagate = propagate
        lg.filters[:] = filters
    ls._state.service, ls._state.configured = state


def lines(out: str, logger: str | None = None) -> list[tuple]:
    parsed = [m.groups() for m in map(LINE.match, out.splitlines()) if m]
    return [p for p in parsed if logger is None or p[2] == logger]


# ---- format -------------------------------------------------------------------------

def test_text_line_format_with_and_without_a_request_id(fresh, capsys):
    fresh()
    log = logging.getLogger("t")
    with ls.bind("rid-1.a_b"):
        log.info("hello %s", ls.kv(a=1))
    log.warning("outside")
    got = lines(capsys.readouterr().out, "t")
    assert got[0][1:] == ("INFO", "t", "svc", "rid-1.a_b", "hello a=1")
    assert got[1][1:] == ("WARNING", "t", "svc", "-", "outside")


def test_timestamps_are_utc_iso_with_milliseconds(fresh, capsys):
    fresh()
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "x", None, None)
    record.created, record.msecs = 0.0, 7.9
    assert ls.TextFormatter().format(record).startswith("1970-01-01T00:00:00.007Z INFO t ")


def test_json_format_is_one_object_per_line_with_the_same_fields(fresh, capsys):
    fresh(LOG_FORMAT="JSON")
    log = logging.getLogger("t")
    with ls.bind("rid-2"):
        log.info("multi\nline %s", ls.kv(k="v w"))
        try:
            raise ValueError("boom")
        except ValueError:
            log.exception("failed")
    out = [line for line in capsys.readouterr().out.splitlines() if '"logger": "t"' in line]
    first, second = (json.loads(line) for line in out)
    assert set(first) == {"ts", "level", "logger", "service", "request_id", "message"}
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", first["ts"])
    assert first["message"] == 'multi\nline k="v w"' and first["request_id"] == "rid-2"
    assert first["service"] == "svc" and first["level"] == "INFO"
    assert "ValueError: boom" in second["exc"]


@pytest.mark.parametrize("env,info_shown,debug_shown", [
    ({}, True, False), ({"LOG_LEVEL": "debug"}, True, True),
    ({"LOG_LEVEL": "WARNING"}, False, False), ({"LOG_LEVEL": "warn"}, False, False),
])
def test_log_level_from_env(fresh, capsys, env, info_shown, debug_shown):
    fresh(**env)
    logging.getLogger("t").info("an info line")
    logging.getLogger("t").debug("a debug line")
    out = capsys.readouterr().out
    assert ("an info line" in out) is info_shown
    assert ("a debug line" in out) is debug_shown


def test_a_bad_level_or_format_warns_and_falls_back(fresh, capsys):
    fresh(LOG_LEVEL="loud", LOG_FORMAT="xml")
    logging.getLogger("t").info("still info")
    out = capsys.readouterr().out
    assert "LOG_LEVEL not recognised; using INFO" in out
    assert "LOG_FORMAT not recognised; using text" in out
    assert "loud" not in out and "xml" not in out          # the value is not echoed
    assert lines(out, "t")[0][5] == "still info"            # text format, INFO level


def test_configure_runs_once_per_process_unless_forced(fresh):
    fresh()
    assert ls.configure("another-service") is False
    assert ls._state.service == "svc"


# ---- uvicorn ------------------------------------------------------------------------

def test_uvicorns_loggers_go_through_the_one_handler(fresh, capsys):
    import uvicorn.config
    # What uvicorn does before it imports the app: its own handlers, no propagation.
    logging.config.dictConfig(uvicorn.config.LOGGING_CONFIG)
    fresh()
    fresh()                                   # twice: still no duplicates
    root = logging.getLogger()
    assert len(root.handlers) == 1 and isinstance(root.handlers[0], ls.ConsoleHandler)
    for name in ("uvicorn", "uvicorn.error"):
        assert logging.getLogger(name).handlers == [] and logging.getLogger(name).propagate
    access = logging.getLogger("uvicorn.access")
    assert sum(isinstance(f, ls.StripQueryString) for f in access.filters) == 1
    logging.getLogger("uvicorn.error").info("Started server process [%d]", 7)
    out = capsys.readouterr().out
    assert out.count("Started server process [7]") == 1
    assert lines(out, "uvicorn.error")[0][1:5] == ("INFO", "uvicorn.error", "svc", "-")


def test_uvicorn_writes_no_access_line_at_all(fresh, capsys):
    """uvicorn's protocols log an access line only when `uvicorn.access` has
    a handler path (hasHandlers(); that is all --no-access-log changes). It
    has none after configure(), so uvicorn's line, which carries the query
    string, is never even created; request_log writes ours."""
    import uvicorn.config
    logging.config.dictConfig(uvicorn.config.LOGGING_CONFIG)    # uvicorn's own, first
    fresh()
    access = logging.getLogger("uvicorn.access")
    assert access.handlers == [] and access.propagate is False
    assert access.hasHandlers() is False
    # Even a direct call (as uvicorn's protocols make it) prints nothing.
    access.info('%s - "%s %s HTTP/%s" %d', "10.0.0.1:5000", "GET",
                "/oauth/callback/google?code=4/0AQSTgQE-secret-code&state=nonce-1", "1.1", 200)
    assert "oauth" not in capsys.readouterr().out


def test_the_strip_filter_cuts_the_query_off_a_uvicorn_access_record():
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
        ("10.0.0.1:5000", "GET", "/oauth/callback/google?code=4/0A-code&state=s", "1.1", 200),
        None)
    assert ls.StripQueryString().filter(record) is True
    assert record.getMessage() == '10.0.0.1:5000 - "GET /oauth/callback/google HTTP/1.1" 200'


def test_noisy_third_party_loggers_are_held_at_warning(fresh, capsys):
    fresh(LOG_LEVEL="DEBUG")
    logging.getLogger("httpx").info('HTTP Request: GET https://gmail.googleapis.com/?q=secret')
    logging.getLogger("mcp.server.lowlevel.server").debug("Received message: {tool args}")
    logging.getLogger("httpx").warning("kept")
    out = capsys.readouterr().out
    assert "q=secret" not in out and "tool args" not in out and "kept" in out


# ---- filters and the handler ----------------------------------------------------------

def test_redaction_returns_a_copy_and_leaves_the_record_alone():
    key = "aab_" + "a" * 48
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "key %s", (key,), None)
    out = ls.RedactSecrets().filter(record)
    assert out is not record and out.getMessage() == "key <redacted>"
    # Other handlers (a test's capture) still see what the code logged.
    assert record.getMessage() == f"key {key}"


def test_redaction_covers_tracebacks_and_stacks():
    try:
        raise RuntimeError("failed with ghp_" + "A" * 36)
    except RuntimeError:
        record = logging.LogRecord("t", logging.ERROR, __file__, 1, "x", None, sys.exc_info())
    record.stack_info = "Stack: Bearer abcdefghijklmnop"
    out = ls.RedactSecrets().filter(record)
    assert out.exc_info is None and "RuntimeError" in out.exc_text
    assert "ghp_" + "A" * 36 not in out.exc_text and "abcdefghijklmnop" not in out.stack_info
    assert "failed with <redacted>" in out.exc_text


def test_the_handler_output_is_redacted(fresh, capsys):
    fresh()
    logging.getLogger("t").info("admin token aab_admin_%s", "b" * 48)
    out = capsys.readouterr().out
    assert "admin token <redacted>" in out and "b" * 48 not in out


def test_the_context_filter_stamps_id_and_service():
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "x", None, None)
    with ls.bind("rid-9"):
        ls.RequestIdFilter().filter(record)
    assert record.request_id == "rid-9" and record.service == ls._state.service


def test_the_handler_follows_sys_stdout(monkeypatch):
    handler = ls.ConsoleHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    handler.emit(logging.LogRecord("t", logging.INFO, __file__, 1, "late", None, None))
    assert buf.getvalue() == "late\n"


# ---- request context -------------------------------------------------------------------

def test_bind_nests_and_restores():
    assert ls.current_request_id() is None
    with ls.bind("outer"):
        with ls.bind("inner") as ctx:
            ls.set_actor("key:a")
            assert ls.current_request_id() == "inner" and ls.current_actor() == "key:a"
        assert ls.current_request_id() == "outer" and ls.current_actor() == "-"
    assert ls.current_request_id() is None
    ls.set_actor("key:ignored")                     # outside a request: a no-op
    assert ctx.actor == "key:a"


@pytest.mark.parametrize("value,ok", [
    ("5f0c" * 8, True), ("sched-" + "a" * 32, True), ("a.b-c_d", True), ("x" * 128, True),
    ("x" * 129, False), ("", False), ("a b", False), ("a\nb", False), ('a"b', False),
    (None, False), ("é", False),
])
def test_request_id_shape(value, ok):
    assert ls.valid_request_id(value) is ok


def test_bind_replaces_a_malformed_id():
    with ls.bind("bad id") as ctx:
        assert re.fullmatch(r"[0-9a-f]{32}", ctx.request_id)


def test_new_request_id_prefix():
    assert re.fullmatch(r"tg-[0-9a-f]{32}", ls.new_request_id("tg-"))


# ---- kv -----------------------------------------------------------------------------------

def test_kv_renders_bare_quoted_and_empty_values():
    assert ls.kv(a=1, b="x", c=None, d="", e=True, f=[3, 1], g=set()) == \
        "a=1 b=x c=- d=- e=true f=3,1 g=-"
    assert ls.kv(path="/v1/targets/echo") == "path=/v1/targets/echo"
    assert ls.kv(reason="no vendored manifest") == 'reason="no vendored manifest"'


def test_kv_values_cannot_break_the_line_or_forge_a_pair():
    out = ls.kv(name='evil" x=1\nFORGED INFO line\r\t\x00 \x85\\')
    assert "\n" not in out and "\r" not in out and " " not in out and "\x85" not in out
    assert out == 'name="evil\\" x=1\\nFORGED INFO line\\r\\t\\u0000\\u2028\\u0085\\\\"'


def test_kv_cuts_long_values():
    out = ls.kv(v="y" * 500)
    assert out == "v=" + "y" * 200 + "..."
