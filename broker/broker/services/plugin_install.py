"""Install, upgrade and remove external plugins: the broker's half of the plugin installer.

The broker never runs Docker and never clones a repository. aab-installer
does both, behind its own boundary (docker-compose.installer.yml: it shares
net_installer with the broker alone, every call carries INSTALLER_TOKEN as
X-Installer-Token, and its source allowlist is read from the host's .env
only). This module is the broker's client for it, and the one place where
the owner's approval of what an installed plugin may do is written:

  status()                     is the installer configured, and does it answer our token?
  inspect(source, ref)         the installer clones and reads the package; the broker
                               validates every manifest with its own strict schema and
                               builds the review card (what each plugin can do, and on
                               an upgrade what changes against the current pin)
  install(ctx, source, ref, commit)
                               inspect again at the reviewed commit, PIN every manifest
                               (plugin.pin, audited) and only then ask for the install
  upgrade(ctx, service, source, ref, commit)
                               the same against an installed service: re-pin, then ask
  remove(ctx, service, purge)  ask for the removal, then unpin every plugin it hosted
  job(id), installed()         passthrough for the console's job panel and cards
  reconcile_services()         apply the installer's GET /services to the registry
  services_loop()              the lifespan loop that keeps that set current

Hot plugins: no install, upgrade or remove recreates the broker. The
installer connects the running broker to the plugin's network, and the
broker learns the service's URL and token from `GET /services` instead of
its environment (plugins/registry.py has the merge rule). reconcile_services
fetches that list and applies it: a new service is registered and discovered,
a service the list no longer names is evicted (agents get 404). It runs at
boot, when a job ends (seen through the job route or the broker's own
polling of the jobs it submitted), and every RECONCILE_SECONDS as a safety
net, in a background thread, never in a request. An installer that is off,
down or answering garbage changes nothing: the previous set stays, and the
problem is logged once, not on every tick. The tokens travel only over
net_installer, in the answer to X-Installer-Token, and live in the
registry's memory: the broker never logs or writes them.

Why pin first: a service the installer starts is registered by ordinary
discovery, which accepts it only if it offers exactly the approved copy. With
the pins written before the job exists, the plugin comes up already approved
(and still disabled until the owner enables it), and nothing the container
does can widen what was reviewed. If the installer refuses the request, the
pins are put back as they were, so a refused upgrade never leaves a running
plugin pinned to a version it does not serve.

Why the manifests are fetched again at install time instead of being taken
from the console's request: the pin must be what the installer will build,
at the commit the owner reviewed. The request carries only source, ref and
commit; a commit that no longer matches is refused (409) before anything is
pinned, and the installer checks the same commit again when its job fetches.

Private repositories: the read-only GitHub token the owner stored in the
console (services/install_git_token.py) travels in the body of inspect,
install and upgrade requests as `git_token`, and only there; the installer
uses it for that request's or that job's clone and never stores it. Nothing
is sent when none is stored; one stored under an older BROKER_SECRETS_KEY
refuses the request (409 git_token_unreadable) before anything is asked or
pinned.

Errors: installer refusals are relayed with their status and code (400, 403
source_not_allowed, 404, 409, 422 invalid_package, 502 clone_failed, 503); an
installer that is off or down answers 503 saying which; a mutation whose
answer was lost is 502 (it may have been accepted: never retried blindly).
A 401 from the installer means the two containers disagree on the token and
becomes a 503: a 401 here would sign the owner out of the console.

Logging (docs/logging.md): sources, refs, commits, services, plugin ids and
job ids. INSTALLER_TOKEN is sent as a header and the GitHub token in a body;
neither is ever logged, audited or returned.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

import anyio
import httpx

from ..audit import audit
from ..config import get_settings
from ..errors import PolicyError
from ..logging_setup import bind, current_request_id, kv, new_request_id
from ..plugins import pins
from ..plugins.manifest import ManifestError, load_manifest_text
from ..plugins.registry import get_registry
from . import install_git_token, plugins_admin

log = logging.getLogger(__name__)

TOKEN_HEADER = "X-Installer-Token"
INSPECT_TIMEOUT = 30.0          # a shallow clone of a small repository
CALL_TIMEOUT = 10.0             # everything else answers at once (jobs run in the background)
# The installer's own rules (installer/aab_installer/descriptor.py, jobs.py, git.py).
SERVICE_RE = re.compile(r"^[a-z][a-z0-9]{1,31}$")
JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
# Installer refusals relayed with their own status; any other status is an
# answer the broker cannot vouch for (502).
_RELAYED = frozenset({400, 403, 404, 409, 422, 502, 503})
# The fields of a job the console sees (installer/aab_installer/jobs.py
# public()). Anything else in the installer's answer is dropped, so no field
# the broker did not expect (an echoed request field above all: the GitHub
# token travels in those requests) can reach a response.
JOB_KEYS = ("id", "kind", "state", "service", "source", "ref", "commit", "purge",
            "created_at", "started_at", "finished_at", "error", "log", "log_truncated")

OFF_MESSAGE = ("the plugin installer is off: set INSTALLER_ENABLED=true and "
               "INSTALLER_ALLOWED_SOURCES in the host's .env, then run "
               "docker compose $(scripts/compose-files.sh) up -d (docs/deployment.md)")
# What the console shows when the installer is off, and next to the source field.
# (The GitHub token for private repositories is not here: it is a console
# setting, services/install_git_token.py.)
ENABLE_LINES = (
    "INSTALLER_ENABLED=true",
    "INSTALLER_ALLOWED_SOURCES=github.com/<you>/*",
)
ALLOWLIST_HINT = ("Only repositories matching INSTALLER_ALLOWED_SOURCES in the host's .env can "
                  "be installed (for example github.com/you/*). It is read from that file "
                  "alone: nothing in this console can widen it. The ref is a release tag "
                  "(v1.2.3) or a full 40-character commit.")
MANUAL_COMMAND = "docker compose $(scripts/compose-files.sh) up -d"

# (base_url, headers, timeout) -> client. Tests replace `client_factory`
# with one that serves a fake installer in process.
ClientFactory = Callable[[str, dict, float], httpx.Client]


def _default_client(base_url: str, headers: dict, timeout: float) -> httpx.Client:
    return httpx.Client(base_url=base_url, headers=headers, timeout=timeout)


client_factory: ClientFactory = _default_client


# ---- the HTTP client -----------------------------------------------------------------

def configured_installer() -> bool:
    """INSTALLER_URL and INSTALLER_TOKEN are both set (env-only settings)."""
    s = get_settings()
    return bool((s.installer_url or "").strip() and (s.installer_token or "").strip())


def _endpoint() -> tuple[str, str]:
    s = get_settings()
    url = (s.installer_url or "").strip().rstrip("/")
    token = (s.installer_token or "").strip()
    if not url:
        raise PolicyError(503, OFF_MESSAGE, "installer_off")
    if not token:
        # Named only: a URL without its token is a broken deployment.
        raise PolicyError(503, "the plugin installer is misconfigured: INSTALLER_URL is set "
                               "but INSTALLER_TOKEN is empty", "installer_off")
    return url, token


def _json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return None


def _refusal(status: int, data: Any) -> PolicyError:
    """The installer's error answer as the broker's error."""
    body = data if isinstance(data, dict) else {}
    message = body.get("error") if isinstance(body.get("error"), str) else f"HTTP {status}"
    code = body.get("code") if isinstance(body.get("code"), str) and CODE_RE.match(
        body["code"]) else None
    if status == 401:
        return PolicyError(503, "the installer refused the broker's INSTALLER_TOKEN: the two "
                                "containers disagree (recreate both from the same .env)",
                           "installer_token_mismatch")
    if status not in _RELAYED:
        return PolicyError(502, f"installer error: {message}"[:1000], code or "installer_error")
    return PolicyError(status, f"installer: {message}"[:2000], code)


def _call(method: str, path: str, body: dict | None = None, *, timeout: float = CALL_TIMEOUT,
          mutation: bool = False, quiet: bool = False) -> dict:
    """One installer call with the error contract applied. `quiet`: no log
    line of its own (the background sync logs a problem once, itself)."""
    url, token = _endpoint()
    headers = {TOKEN_HEADER: token, "Accept": "application/json"}
    request_id = current_request_id()
    if request_id:
        headers["X-Request-Id"] = request_id         # one id across broker and installer lines
    try:
        with client_factory(url, headers, timeout) as client:
            resp = client.request(method, path, json=body)
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        if not quiet:
            log.warning("installer unreachable %s", kv(method=method, path=path,
                                                       error=type(exc).__name__))
        raise PolicyError(503, f"the plugin installer is down or unreachable "
                               f"({type(exc).__name__}): is aab-installer running?",
                          "installer_down") from exc
    except httpx.HTTPError as exc:
        if not quiet:
            log.warning("installer call failed %s", kv(method=method, path=path,
                                                       error=type(exc).__name__))
        if mutation:
            # The request may have reached it: a job may exist. Never retried.
            raise PolicyError(502, "the installer did not answer; the request may have been "
                                   "accepted: check Installed and the job before retrying",
                              "unknown_outcome") from exc
        raise PolicyError(503, f"the plugin installer did not answer in time "
                               f"({type(exc).__name__}); try again", "installer_down") from exc
    data = _json(resp)
    if resp.status_code >= 400:
        err = _refusal(resp.status_code, data)
        if not quiet:
            log.info("installer refused %s", kv(method=method, path=path,
                                                status=resp.status_code, code=err.code))
        raise err
    if not isinstance(data, dict):
        raise PolicyError(502, "the installer returned an unexpected answer", "installer_error")
    return data


def _audit(ctx, action: str, resource: str, detail: dict, result: str = "ok") -> None:
    audit(ctx.username, action, resource, detail, result,
          actor_principal=ctx.principal_id, actor_via=ctx.via)


def _service(service: object) -> str:
    if not isinstance(service, str) or not SERVICE_RE.match(service):
        raise PolicyError(400, "service must be an installed plugin service name", "bad_request")
    return service


# ---- status and passthrough --------------------------------------------------------------

def status() -> dict:
    """Whether the installer is configured and answers the broker's token,
    plus what the console shows around the Add plugin dialog: the hints, and
    whether a GitHub token is stored (a state word). Never a token or the
    installer's address."""
    s = get_settings()
    configured = configured_installer()
    out = {"configured": configured, "reachable": False, "installed": None, "error": None,
           "allowlist_hint": ALLOWLIST_HINT, "enable_lines": list(ENABLE_LINES),
           "manual_command": MANUAL_COMMAND, **install_git_token.view()}
    if not configured:
        out["error"] = OFF_MESSAGE if not (s.installer_url or "").strip() else (
            "INSTALLER_URL is set but INSTALLER_TOKEN is empty")
        return out
    try:
        items = _call("GET", "/installed").get("items")
    except PolicyError as exc:
        out["error"] = str(exc)
        return out
    out.update(reachable=True, installed=len(items) if isinstance(items, list) else 0)
    return out


def installed() -> dict:
    """{"items": [install record]} as the installer keeps them."""
    items = _call("GET", "/installed").get("items")
    return {"items": [r for r in items if isinstance(r, dict)] if isinstance(items, list) else []}


def _job_view(data: dict) -> dict:
    """A job answer projected to JOB_KEYS (see there)."""
    return {k: data.get(k) for k in JOB_KEYS}


def job(job_id: str) -> dict:
    """A job, for the console's panel. A job seen ending here is handed to
    the sync loop (job_ended), which applies its result off the request."""
    if not isinstance(job_id, str) or not JOB_ID_RE.match(job_id):
        raise PolicyError(404, "no such job", "not_found")      # never a path built from junk
    view = _job_view(_call("GET", f"/jobs/{job_id}"))
    job_ended(view)
    return view


# ---- inspect: the review card ------------------------------------------------------------

def _current_pin(plugin_id: str):
    try:
        return pins.get(plugin_id)
    except ManifestError:
        return None                        # an unreadable pin diffs as a first pin


def _inspected(source: str, ref: str, git: dict) -> tuple[dict, dict[str, str]]:
    """Ask the installer to inspect, validate what it returned, and build the
    review. `git` is install_git_token.request_fields(): the GitHub token's
    field when one is stored. Returns (review, {plugin id: manifest text}
    for the valid ones)."""
    raw = _call("POST", "/inspect", {"source": source, "ref": ref, **git},
                timeout=INSPECT_TIMEOUT)
    desc, entries = raw.get("descriptor"), raw.get("manifests")
    commit, src = raw.get("commit"), raw.get("source")
    if (not isinstance(desc, dict) or not isinstance(entries, list) or not isinstance(src, str)
            or not isinstance(commit, str) or not COMMIT_RE.match(commit)):
        raise PolicyError(502, "the installer returned an unexpected inspect answer",
                          "installer_error")
    service, declared = desc.get("service"), desc.get("plugins")
    if (not isinstance(service, str) or not SERVICE_RE.match(service)
            or not isinstance(declared, list) or not all(isinstance(p, str) for p in declared)):
        raise PolicyError(502, "the installer returned an unexpected descriptor",
                          "installer_error")
    items, texts, seen = [], {}, []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("plugin"), str):
            raise PolicyError(502, "the installer returned an unexpected manifest entry",
                              "installer_error")
        pid, text = entry["plugin"], entry.get("text")
        seen.append(pid)
        item = {"id": pid, "path": entry.get("path"), "version": entry.get("version"),
                "valid": False, "error": None, "summary": None, "diff": None,
                "pinned": pins.record(pid), "blocked": None}
        try:
            if not isinstance(text, str):
                raise ManifestError("the manifest is missing")
            m = load_manifest_text(text)
            if m.id != pid:
                raise ManifestError(f"manifest declares id {m.id!r}, not {pid!r}")
        except ManifestError as exc:
            item["error"] = str(exc)[:1000]
        else:
            item.update(valid=True, version=m.version, summary=pins.summary(m),
                        diff=pins.diff(_current_pin(pid), m),
                        blocked=plugins_admin.pin_blocker(pid, service))
            texts[pid] = text
        items.append(item)
    if sorted(seen) != sorted(declared) or len(set(seen)) != len(seen):
        raise PolicyError(502, "the package's manifests do not match its descriptor",
                          "installer_error")
    record = raw.get("installed") if isinstance(raw.get("installed"), dict) else None
    problems = [f"{i['id']}: {i['error']}" for i in items if not i["valid"]]
    problems += [f"{i['id']}: {i['blocked']}" for i in items if i["blocked"]]
    review = {"source": src, "ref": raw.get("ref", ref), "commit": commit, "service": service,
              "descriptor": desc, "installed": record, "upgrade": record is not None,
              "plugins": items, "problems": problems}
    return review, texts


def inspect(source: str, ref: str) -> dict:
    review, _ = _inspected(source, ref, install_git_token.request_fields())
    log.info("plugin package inspected %s", kv(
        source=review["source"], ref=review["ref"], commit=review["commit"],
        service=review["service"], plugins=[i["id"] for i in review["plugins"]],
        upgrade=review["upgrade"], problems=len(review["problems"])))
    return review


# ---- install, upgrade, remove -----------------------------------------------------------

def _check_reviewed(review: dict, commit: str) -> None:
    if review["commit"] != commit:
        raise PolicyError(409, f"{review['ref']} is now commit {review['commit']}, not the "
                               f"reviewed {commit}: inspect it again", "commit_changed")
    invalid = [i for i in review["plugins"] if not i["valid"]]
    if invalid:
        raise PolicyError(400, "invalid manifest: " + "; ".join(
            f"{i['id']}: {i['error']}" for i in invalid)[:2000], "invalid_manifest")
    blocked = [i for i in review["plugins"] if i["blocked"]]
    if blocked:
        raise PolicyError(409, "; ".join(f"{i['id']}: {i['blocked']}" for i in blocked),
                          "conflict")


def _restore(snapshots: dict[str, dict | None], registered: list[str]) -> list[str]:
    """Put every pin back as it was before this request."""
    for pid, snap in snapshots.items():
        pins.restore(pid, snap)
    for pid in registered:
        # It came up under the pin just undone: off until the owner reviews it.
        get_registry().withdraw(pid, "not pinned: the install request was refused")
    return sorted(snapshots)


def _refused(ctx, action: str, resource: str, detail: dict, exc: PolicyError,
             **extra) -> None:
    """Audit and log a mutation that did not happen (or whose outcome is
    unknown): the owner asked, and the record says so either way."""
    _audit(ctx, action, resource, {**detail, "status": exc.status, "code": exc.code, **extra},
           "error")
    log.warning("plugin %s refused %s", action.split(".")[1], kv(
        resource=resource, status=exc.status, code=exc.code, by=ctx.username, via=ctx.via,
        **extra))


def _pin_then_call(ctx, action: str, review: dict, texts: dict[str, str], path: str,
                   body: dict, detail: dict) -> dict:
    service = review["service"]
    snapshots = {pid: pins.snapshot(pid) for pid in texts}
    registered: list[str] = []
    try:
        for pid, text in texts.items():
            out = plugins_admin.pin_manifest(ctx, pid, text, source=review["source"],
                                             ref=review["ref"], commit=review["commit"],
                                             service=service)
            if out.get("registered"):
                registered.append(pid)
        submitted = _job_view(_call("POST", path, body, mutation=True))
    except PolicyError as exc:
        # A lost answer may still have queued the job: keep the pins it needs.
        restored = [] if exc.code == "unknown_outcome" else _restore(snapshots, registered)
        _refused(ctx, action, service, detail, exc, pins_restored=restored)
        raise
    except Exception:
        # Anything unexpected before the installer accepted: fail closed, no
        # pin may outlive a request that did not happen.
        restored = _restore(snapshots, registered)
        _refused(ctx, action, service, detail, PolicyError(500, "internal error", "internal"),
                 pins_restored=restored)
        raise
    watch_job(submitted)                     # its end is acted on even with no console open
    _audit(ctx, action, service, {**detail, "job": submitted.get("id"),
                                  "plugins": sorted(texts)})
    log.info("plugin %s requested %s", action.split(".")[1], kv(
        service=service, source=review["source"], ref=review["ref"], commit=review["commit"],
        plugins=sorted(texts), job=submitted.get("id"), by=ctx.username, via=ctx.via))
    return submitted


def install(ctx, source: str, ref: str, commit: str) -> dict:
    """Pin the reviewed package's manifests, then ask the installer to install it."""
    detail, resource = {"source": source, "ref": ref, "commit": commit}, source
    try:
        git = install_git_token.request_fields()
        review, texts = _inspected(source, ref, git)
        resource = review["service"]
        detail.update(source=review["source"], ref=review["ref"])
        if review["installed"] is not None:
            raise PolicyError(409, f"{resource} is already installed; upgrade it instead",
                              "already_installed")
        _check_reviewed(review, commit)
    except PolicyError as exc:
        _refused(ctx, "plugin.install", resource, detail, exc)
        raise
    submitted = _pin_then_call(ctx, "plugin.install", review, texts, "/install",
                               {"source": review["source"], "ref": review["ref"],
                                "commit": commit, **git}, detail)
    return {"job": submitted, "service": review["service"], "pinned": sorted(texts)}


def upgrade(ctx, service: str, source: str, ref: str, commit: str) -> dict:
    """Re-pin the manifests of the new version, then ask for the upgrade.
    Plugin ids the new version no longer hosts are unpinned once accepted."""
    service = _service(service)
    detail = {"source": source, "ref": ref, "commit": commit}
    try:
        git = install_git_token.request_fields()
        review, texts = _inspected(source, ref, git)
        detail.update(source=review["source"], ref=review["ref"])
        record = review["installed"]
        if review["service"] != service:
            raise PolicyError(409, f"the package at {review['ref']} is service "
                                   f"{review['service']}, not {service}", "service_changed")
        if record is None:
            raise PolicyError(404, f"{service} is not installed", "not_installed")
        if record.get("source") != review["source"]:
            raise PolicyError(409, f"{service} is installed from {record.get('source')}; "
                                   "remove it to install from another source",
                              "source_changed")
        _check_reviewed(review, commit)
    except PolicyError as exc:
        _refused(ctx, "plugin.upgrade", service, detail, exc)
        raise
    detail["from"] = {"ref": record.get("ref"), "commit": record.get("commit")}
    submitted = _pin_then_call(ctx, "plugin.upgrade", review, texts, "/upgrade",
                               {"service": service, "source": review["source"],
                                "ref": review["ref"], "commit": commit, **git}, detail)
    dropped = [pid for pid in record.get("plugins") or []
               if isinstance(pid, str) and pid not in texts and pins.record(pid) is not None]
    for pid in dropped:
        plugins_admin.unpin(ctx, pid)
    return {"job": submitted, "service": service, "pinned": sorted(texts), "unpinned": dropped}


def remove(ctx, service: str, purge: bool = False) -> dict:
    """Ask the installer to remove `service`, then unpin every plugin it
    hosted (each stops being served at once and its row is disabled)."""
    service = _service(service)
    detail = {"purge": bool(purge)}
    try:
        record = next((r for r in installed()["items"] if r.get("service") == service), None)
        if record is None:
            raise PolicyError(404, f"{service} is not installed", "not_installed")
        detail.update(source=record.get("source"), ref=record.get("ref"),
                      commit=record.get("commit"))
        submitted = _job_view(_call("POST", "/remove", {"service": service,
                                                        "purge": bool(purge)}, mutation=True))
    except PolicyError as exc:
        _refused(ctx, "plugin.remove", service, detail, exc)
        raise
    watch_job(submitted)
    unpinned = []
    for pid in record.get("plugins") or []:
        if isinstance(pid, str) and pins.record(pid) is not None:
            plugins_admin.unpin(ctx, pid)
            unpinned.append(pid)
    _audit(ctx, "plugin.remove", service, {**detail, "job": submitted.get("id"),
                                           "unpinned": unpinned})
    log.info("plugin remove requested %s", kv(service=service, purge=bool(purge),
                                              job=submitted.get("id"), unpinned=unpinned,
                                              by=ctx.username, via=ctx.via))
    return {"job": submitted, "service": service, "unpinned": unpinned}


# ---- hot plugins: the installer's services, applied while the broker runs -----------------

RECONCILE_SECONDS = 60      # the safety net: GET /services at least this often
WATCH_TICK = 2.0            # the sync loop's tick (cheap: no call unless one is due)
WATCH_SECONDS = 1800        # a submitted job is followed at most this long (a long build)
FAST_RETRY_SECONDS = 120    # after a job ends, its booting plugin is retried every tick
SERVICE_KEYS = frozenset({"service", "url", "token"})
BACKGROUND_PREFIX = "inst-"  # the sync loop's request ids (main.BACKGROUND_ID_PREFIXES)

_sync_lock = threading.Lock()        # guards _sync
_reconcile_lock = threading.Lock()   # one reconcile at a time: boot, the loop and job ends
_sync: dict[str, Any] = {}


def reset_sync() -> None:
    """Forget the sync state (a fresh boot; tests)."""
    with _sync_lock:
        _sync.clear()
        # watched: job id -> follow deadline. ended: job id -> what to do
        # once the installer is readable. handled: job ids acted on (each
        # once). fast: service -> deadline of its quick retries.
        _sync.update(next=0.0, problem=None, watched={}, ended={},
                     handled=deque(maxlen=64), fast={})


reset_sync()


def _parse_services(data: dict) -> dict[str, tuple[str, str]]:
    """GET /services as {service: (url, token)}; the registry validates the
    names, URLs and tokens. Raises ValueError (never echoing a value)."""
    items = data.get("items")
    if not isinstance(items, list):
        raise ValueError("the answer has no list of items")
    out: dict[str, tuple[str, str]] = {}
    for item in items:
        if not isinstance(item, dict) or set(item) != SERVICE_KEYS:
            raise ValueError("an item is not exactly {service, url, token}")
        service = item["service"]
        if not isinstance(service, str) or not SERVICE_RE.match(service):
            raise ValueError("an item's service name is malformed")
        if service in out:
            raise ValueError(f"service {service!r} is listed twice")
        out[service] = (item["url"], item["token"])
    return out


def _problem(problem: str | None) -> None:
    """Log a sync problem once (not on every tick), and its end."""
    with _sync_lock:
        last, _sync["problem"] = _sync["problem"], problem
    if problem and problem != last:
        log.warning("installer services not applied; keeping the current set %s",
                    kv(reason=problem))
    elif problem is None and last:
        log.info("installer services applied again")


def reconcile_services() -> dict | None:
    """Fetch the installer's GET /services and apply it to the registry:
    new services are registered and discovered, services it no longer lists
    are evicted. Returns {services, discovered, removed} (names only), or
    None when the installer is not configured or the answer was not usable,
    in which case nothing changed. Blocking (network calls): run it in a
    thread, never in a request."""
    if not configured_installer():
        return None
    with _reconcile_lock:
        with _sync_lock:
            _sync["next"] = time.monotonic() + RECONCILE_SECONDS
        reg = get_registry()
        try:
            change = reg.set_dynamic_services(_parse_services(
                _call("GET", "/services", quiet=True)))
        except PolicyError as exc:
            _problem(f"installer answered {exc.status} {exc.code}")
            return None
        except ValueError as exc:
            _problem(f"unexpected answer: {exc}")
            return None
        _problem(None)
        if change.fresh:
            reg.discover(change.fresh)
        out = {"services": reg.dynamic_services(), "discovered": sorted(change.fresh),
               "removed": list(change.removed)}
        if change.fresh or change.removed:
            log.info("installer services applied %s", kv(**out))
        return out


def watch_job(job: dict) -> None:
    """Follow a job this broker submitted, so its end is acted on even when
    no console polls it. Never raises: the job is already queued."""
    job_id = job.get("id") if isinstance(job, dict) else None
    if isinstance(job_id, str) and JOB_ID_RE.match(job_id):
        with _sync_lock:
            _sync["watched"][job_id] = time.monotonic() + WATCH_SECONDS


def job_ended(view: dict) -> None:
    """Hand a job seen done or failed to the sync loop (once per job). An
    install or upgrade started a new container, so its service is
    discovered again; a remove changed the list; a failure may have rolled
    either back. All of it waits for the loop: no network call here."""
    job_id = view.get("id")
    if (view.get("state") not in ("done", "failed") or not isinstance(job_id, str)
            or not JOB_ID_RE.match(job_id)):
        return
    service = view.get("service")
    with _sync_lock:
        _sync["watched"].pop(job_id, None)
        if job_id in _sync["handled"] or job_id in _sync["ended"]:
            return
        _sync["ended"][job_id] = {
            "kind": view.get("kind"),
            "service": service if isinstance(service, str) and SERVICE_RE.match(service)
            else None,
            "until": time.monotonic() + WATCH_SECONDS}


def _poll_watched(now: float) -> None:
    with _sync_lock:
        watched = list(_sync["watched"].items())
    for job_id, deadline in watched:
        if now > deadline:
            with _sync_lock:
                _sync["watched"].pop(job_id, None)
            continue
        try:
            view = _job_view(_call("GET", f"/jobs/{job_id}", quiet=True))
        except PolicyError as exc:
            if exc.status == 404:                   # the installer forgot it: stop
                with _sync_lock:
                    _sync["watched"].pop(job_id, None)
            continue                                # down: keep following
        job_ended(view)


def _retry_booting(now: float) -> None:
    """Retry, every tick, the services of jobs that just ended and are still
    pending (a container that started but does not answer yet)."""
    with _sync_lock:
        fast = dict(_sync["fast"])
    if not fast:
        return
    reg = get_registry()
    pending = set(reg.pending_services())
    retry = sorted(s for s, until in fast.items() if now <= until and s in pending)
    with _sync_lock:
        for service, until in fast.items():
            if now > until or service not in pending:
                _sync["fast"].pop(service, None)
    if retry:
        reg.discover_named(retry)


def _apply_ended(now: float) -> None:
    with _sync_lock:
        ended = dict(_sync["ended"])
    if not ended:
        return
    result = reconcile_services()
    if result is None:
        # The installer is not readable now: try again on the next tick,
        # until the entries expire (the safety net catches up after that).
        with _sync_lock:
            for job_id, entry in ended.items():
                if now > entry["until"]:
                    _sync["ended"].pop(job_id, None)
        return
    restarted = sorted({e["service"] for e in ended.values()
                        if e["kind"] in ("install", "upgrade") and e["service"]})
    # A new container behind an unchanged URL and token (an upgrade, or an
    # install that was listed mid-job) is discovered again here; one the
    # reconcile just discovered is not asked twice.
    get_registry().discover_named([s for s in restarted if s not in result["discovered"]])
    with _sync_lock:
        for job_id in ended:
            _sync["ended"].pop(job_id, None)
            _sync["handled"].append(job_id)
        for service in restarted:
            _sync["fast"][service] = now + FAST_RETRY_SECONDS
    log.info("installer jobs applied %s", kv(jobs=sorted(ended), discovered=restarted))


def services_tick() -> None:
    """One pass of the sync loop: quick retries of booting plugins, the
    jobs this broker follows, jobs seen ending, and the periodic reconcile
    when it is due. Blocking: the loop runs it in a thread."""
    if not configured_installer():
        return
    now = time.monotonic()
    _retry_booting(now)
    _poll_watched(now)
    _apply_ended(now)
    with _sync_lock:
        due = now >= _sync["next"]
    if due:
        reconcile_services()


def _bound_tick() -> None:
    with bind(new_request_id(BACKGROUND_PREFIX)):
        services_tick()


async def services_loop() -> None:
    """The lifespan loop (background.Loop) that keeps the installer's
    services current while the installer is configured."""
    log.info("installer services sync started %s", kv(tick_seconds=WATCH_TICK,
                                                      reconcile_seconds=RECONCILE_SECONDS))
    while True:
        await asyncio.sleep(WATCH_TICK)
        try:
            await anyio.to_thread.run_sync(_bound_tick)
        except asyncio.CancelledError:
            log.info("installer services sync stopped")
            raise
        except Exception as exc:
            # The type only: an exception's text could carry an answer.
            log.error("installer services sync failed %s", kv(error=type(exc).__name__))
