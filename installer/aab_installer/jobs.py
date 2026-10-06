"""Install, upgrade and remove run as jobs: one at a time, persisted, with a capped log.

Every mutation is asynchronous because it recreates the broker (its
environment changes), which is the process the console is polling through:
the broker answers `POST /install` with a job id and polls `GET /jobs/<id>`
across its own restart. So job state lives on disk,
`plugins.d/_installer/jobs/<id>.json`, written atomically after every
change, and survives an installer restart (a job that was queued or running
when the installer stopped is marked failed on the next start: whatever it
was doing did not finish under supervision).

One worker thread runs jobs in order, and `submit` refuses while one is
queued or running: two compose runs against one project never interleave.

Log lines are for the owner: steps, commands (argv carries no secret), exit
codes and a short, redacted tail of command output. Never a token: every line
passes the shared redaction backstop, 64-hex runs (the shape of every
plugin token) are masked, at the cost of image digests, and so is every
value the store was told is secret, whatever its shape: INSTALLER_TOKEN for
every job, and a job's own secrets (the GitHub token its request carried,
`submit(..., mask=...)`) for that job. A job's secrets live in memory only,
beside its work, and are dropped when it finishes: the job record never
holds them, and a job is never re-run after a restart, so nothing needs them
later.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from .logging_setup import bind, kv, redact

log = logging.getLogger(__name__)

STATES = ("queued", "running", "done", "failed")
KINDS = ("install", "upgrade", "remove")
MAX_LOG_LINES = 300
MAX_LINE = 500
JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_HEX64 = re.compile(r"[0-9a-fA-F]{64}")


class Busy(Exception):
    """Another job is queued or running."""


def clean_line(text: str, mask: tuple[str, ...] = ()) -> str:
    """A log line safe to store and show: one line, capped, redacted, and
    with every literal in `mask` replaced."""
    line = " ".join(str(text).split())                  # no newlines or control runs
    for secret in mask:
        line = line.replace(secret, "<redacted>")
    line = _HEX64.sub("<hex64>", redact(line))
    return line[:MAX_LINE]


def _masks(values) -> tuple[str, ...]:
    """Distinct non-empty secrets, longest first, so a secret containing
    another is masked whole."""
    return tuple(sorted({m for m in values if m}, key=len, reverse=True))


class JobContext:
    """What a running job uses to report: `log(line)` and `update(**fields)`.
    `mask` is the job's own secrets (memory only; never saved)."""

    def __init__(self, store: "JobStore", job: dict, mask: tuple[str, ...] = ()):
        self._store, self.job, self._mask = store, job, mask

    def log(self, text: str) -> None:
        lines = self.job["log"]
        lines.append(self._store.clean(text, self._mask))
        if len(lines) > MAX_LOG_LINES:
            # Keep the beginning (what was asked) and the end (how it ended).
            del lines[20:len(lines) - (MAX_LOG_LINES - 20)]
            self.job["log_truncated"] = True
        self._store.save(self.job)

    def update(self, **fields) -> None:
        self.job.update(fields)
        self._store.save(self.job)


class JobStore:
    """Job files plus the single worker. `mask`: secret values that must never
    reach a stored line of any job, whatever their shape (empty strings are
    ignored); `submit(..., mask=...)` adds one job's own."""

    def __init__(self, directory: Path, mask: tuple[str, ...] = ()):
        self._mask = _masks(mask)
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.dir, 0o700)
        except OSError:
            pass
        self._lock = threading.Lock()
        self._queue: queue.Queue = queue.Queue()
        # job id -> (work, the job's own secrets). Memory only: popped when
        # the job runs, so a secret lives exactly as long as its job.
        self._work: dict[str, tuple[Callable[[JobContext], None], tuple[str, ...]]] = {}
        self._worker: threading.Thread | None = None
        self._recover()

    def clean(self, text: str, extra: tuple[str, ...] = ()) -> str:
        return clean_line(text, _masks((*self._mask, *extra)) if extra else self._mask)

    # ---- persistence -------------------------------------------------------------

    def _path(self, job_id: str) -> Path:
        return self.dir / f"{job_id}.json"

    def save(self, job: dict) -> None:
        data = json.dumps(job, sort_keys=True, indent=1).encode("utf-8")
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".job-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, self._path(job["id"]))
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def get(self, job_id: str) -> dict | None:
        if not isinstance(job_id, str) or not JOB_ID_RE.match(job_id):
            return None                                  # never build a path from junk
        try:
            return json.loads(self._path(job_id).read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return None

    def all(self) -> list[dict]:
        out = []
        for p in self.dir.glob("*.json"):
            job = self.get(p.stem)
            if job:
                out.append(job)
        return sorted(out, key=lambda j: (j.get("created_at", 0), j["id"]))

    def _recover(self) -> None:
        for job in self.all():
            if job.get("state") in ("queued", "running"):
                job.update(state="failed", finished_at=int(time.time()),
                           error="the installer restarted before this job finished")
                job["log"].append("installer restarted; job abandoned")
                self.save(job)

    # ---- running -----------------------------------------------------------------

    def active(self) -> dict | None:
        return next((j for j in self.all() if j.get("state") in ("queued", "running")), None)

    def submit(self, kind: str, params: dict, work: Callable[[JobContext], None],
               mask: tuple[str, ...] = ()) -> dict:
        """Queue a job; raises Busy while another one is queued or running.
        `params` become the job record (never put a secret there); `mask` is
        the job's own secrets, masked in its lines and never saved."""
        if kind not in KINDS:
            raise ValueError(f"unknown job kind {kind!r}")
        with self._lock:
            if self.active() is not None:
                raise Busy()
            job = {"id": uuid.uuid4().hex, "kind": kind, "state": "queued",
                   "service": params.get("service"), "source": params.get("source"),
                   "ref": params.get("ref"), "commit": params.get("commit"),
                   "purge": bool(params.get("purge", False)),
                   "created_at": int(time.time()), "started_at": None, "finished_at": None,
                   "error": None, "log": [], "log_truncated": False}
            self.save(job)
            self._work[job["id"]] = (work, _masks(mask))
            self._queue.put(job["id"])
            self._ensure_worker()
        log.info("job queued %s", kv(job=job["id"], kind=kind, service=job["service"],
                                     source=job["source"], ref=job["ref"]))
        return job

    def _ensure_worker(self) -> None:
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._loop, name="installer-jobs",
                                            daemon=True)
            self._worker.start()

    def _loop(self) -> None:
        while True:
            job_id = self._queue.get()
            try:
                self.run(job_id)
            finally:
                self._queue.task_done()

    def run(self, job_id: str) -> dict:
        """Run one queued job to completion (the worker calls this)."""
        job = self.get(job_id)
        entry = self._work.pop(job_id, None)
        if job is None or entry is None:
            return job or {}
        work, mask = entry
        ctx = JobContext(self, job, mask)
        with bind(f"job-{job_id}"):
            ctx.update(state="running", started_at=int(time.time()))
            started = time.monotonic()
            try:
                work(ctx)
            except Exception as exc:                  # the job fails; the worker lives on
                message = self.clean(str(exc) or type(exc).__name__, mask)
                ctx.log(f"failed: {message}")
                ctx.update(state="failed", error=message, finished_at=int(time.time()))
                log.warning("job failed %s", kv(job=job_id, kind=job["kind"],
                                                service=job.get("service"),
                                                error=type(exc).__name__))
            else:
                ctx.update(state="done", finished_at=int(time.time()))
                log.info("job done %s", kv(job=job_id, kind=job["kind"],
                                           service=job.get("service"),
                                           duration_s=round(time.monotonic() - started)))
        return ctx.job

    def wait_idle(self, timeout: float = 30.0) -> bool:
        """Block until no job is queued or running (tests and shutdown)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._queue.unfinished_tasks == 0:
                return True
            time.sleep(0.01)
        return False


def public(job: dict) -> dict:
    """The job as the API returns it."""
    keys = ("id", "kind", "state", "service", "source", "ref", "commit", "purge",
            "created_at", "started_at", "finished_at", "error", "log", "log_truncated")
    return {k: job.get(k) for k in keys}
