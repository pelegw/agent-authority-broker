"""The job store: persisted state, one job at a time, recovery after a
restart, a capped log, and log lines that can carry no token."""

import json
import threading

import pytest

from aab_installer.jobs import MAX_LINE, MAX_LOG_LINES, Busy, JobStore, clean_line, public


def test_a_job_runs_and_is_persisted(tmp_path):
    store = JobStore(tmp_path / "jobs")

    def work(ctx):
        ctx.log("step one")
        ctx.update(service="finance")

    job = store.submit("install", {"source": "github.com/a/b", "ref": "v0.1.0"}, work)
    assert job["state"] == "queued"
    assert store.wait_idle()
    done = store.get(job["id"])
    assert done["state"] == "done" and done["service"] == "finance"
    assert done["log"] == ["step one"] and done["error"] is None
    assert done["started_at"] and done["finished_at"]
    on_disk = json.loads((tmp_path / "jobs" / f"{job['id']}.json").read_text())
    assert on_disk == done
    assert set(public(done)) == {"id", "kind", "state", "service", "source", "ref", "commit",
                                 "purge", "created_at", "started_at", "finished_at", "error",
                                 "log", "log_truncated"}


def test_a_failing_job_is_failed_and_the_worker_lives_on(tmp_path):
    store = JobStore(tmp_path / "jobs")

    def boom(ctx):
        raise RuntimeError("compose up failed (exit 1)")

    failed = store.submit("install", {}, boom)
    assert store.wait_idle()
    job = store.get(failed["id"])
    assert job["state"] == "failed" and job["error"] == "compose up failed (exit 1)"
    ok = store.submit("remove", {"service": "x"}, lambda ctx: None)
    assert store.wait_idle()
    assert store.get(ok["id"])["state"] == "done"


def test_one_job_at_a_time(tmp_path):
    store = JobStore(tmp_path / "jobs")
    gate = threading.Event()
    store.submit("install", {}, lambda ctx: gate.wait(10))
    with pytest.raises(Busy):
        store.submit("install", {}, lambda ctx: None)
    gate.set()
    assert store.wait_idle()
    store.submit("install", {}, lambda ctx: None)                 # free again
    assert store.wait_idle()


def test_unfinished_jobs_fail_on_restart(tmp_path):
    """A job queued or running when the installer stopped did not finish
    under supervision: the next start marks it failed, and frees the queue."""
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    ids = {"queued": "a" * 32, "running": "b" * 32, "done": "c" * 32}
    for state, job_id in ids.items():
        (jobs / f"{job_id}.json").write_text(json.dumps({
            "id": job_id, "kind": "install", "state": state, "log": ["fetching"],
            "created_at": 1}))
    store = JobStore(jobs)
    for state in ("queued", "running"):
        job = store.get(ids[state])
        assert job["state"] == "failed" and "restarted" in job["error"], state
        assert job["log"][-1] == "installer restarted; job abandoned"
    assert store.get(ids["done"])["state"] == "done"
    assert store.active() is None
    store.submit("install", {}, lambda ctx: None)                 # the queue is free
    assert store.wait_idle()


def test_unknown_or_junk_ids_are_none(tmp_path):
    store = JobStore(tmp_path / "jobs")
    for junk in ("", "x", "../" * 10, "0" * 31, None, 5):
        assert store.get(junk) is None
    with pytest.raises(ValueError):
        store.submit("format-disk", {}, lambda ctx: None)


def test_the_log_is_capped_and_keeps_both_ends(tmp_path):
    store = JobStore(tmp_path / "jobs")

    def chatty(ctx):
        for i in range(MAX_LOG_LINES + 100):
            ctx.log(f"line {i}")

    job = store.submit("install", {}, chatty)
    assert store.wait_idle()
    log = store.get(job["id"])["log"]
    assert len(log) == MAX_LOG_LINES and store.get(job["id"])["log_truncated"] is True
    assert log[0] == "line 0" and log[-1] == f"line {MAX_LOG_LINES + 99}"


@pytest.mark.parametrize("text,absent", [
    ("token " + "ab" * 32, "ab" * 32),                            # every generated token's shape
    ("PLUGIN_TOKEN_X=" + "0f" * 32, "0f" * 32),
    ("Authorization: Bearer abcdefghijklmnop", "abcdefghijklmnop"),
    ("X-Installer-Token: s3cr3t-value", "s3cr3t-value"),
    ("key " + "A" * 43 + "=", "A" * 43 + "="),                    # a Fernet key
])
def test_log_lines_are_redacted(text, absent):
    assert absent not in clean_line(text)


def test_log_lines_are_one_capped_line():
    assert clean_line("a\nb\tc") == "a b c"
    assert len(clean_line("x" * 5000)) == MAX_LINE


def test_configured_secrets_are_masked_whatever_their_shape(tmp_path):
    planted = "Planted-0123456789-xyzTOKEN"          # no redaction row matches this shape
    assert planted in clean_line(f"output {planted}")
    store = JobStore(tmp_path / "jobs", mask=(planted, "", planted + "LONGER"))

    def work(ctx):
        ctx.log(f"remote said {planted}LONGER and {planted}")
        raise RuntimeError(f"failed near {planted}")

    job = store.submit("install", {}, work)
    assert store.wait_idle()
    done = store.get(job["id"])
    text = json.dumps(done)
    assert planted not in text and "LONGER" not in text
    assert done["log"][0] == "remote said <redacted> and <redacted>"
    assert done["error"] == "failed near <redacted>"


def test_a_jobs_own_secret_is_masked_in_its_lines_and_never_kept(tmp_path):
    """The GitHub token a request carried is that job's mask: masked in its
    lines and its error, never written to its file, and gone from memory
    once the job has run (a later job is not masked for it: nothing kept it)."""
    planted = "Per-Job-Secret-0123456789xyz"           # no redaction row matches this shape
    store = JobStore(tmp_path / "jobs", mask=("store-wide-secret-value",))

    def work(ctx):
        ctx.log(f"git said {planted} and store-wide-secret-value")
        raise RuntimeError(f"failed near {planted}")

    job = store.submit("install", {"source": "github.com/a/b"}, work, mask=(planted, ""))
    assert store.wait_idle()
    done = store.get(job["id"])
    assert done["log"][0] == "git said <redacted> and <redacted>"
    assert done["error"] == "failed near <redacted>"
    for f in (tmp_path / "jobs").iterdir():
        assert planted not in f.read_text(encoding="utf-8"), f
    assert store._work == {}
    later = store.submit("install", {}, lambda ctx: ctx.log(f"later {planted}"))
    assert store.wait_idle()
    assert store.get(later["id"])["log"] == [f"later {planted}"]
