"""The installer API end to end, without Docker: the token guard, the source
allowlist and ref rules, inspect against a local bare repository, the
install, upgrade and remove jobs (the exact compose commands they run, the
files they leave, the .env secrets, rollback on failure, one job at a time),
and the GitHub token a request may carry (to git through askpass only, kept
nowhere, a malformed one refused unseen)."""

import json
import os
import stat

import pytest
import yaml
from fastapi.testclient import TestClient

from aab_installer import envfile, overlay
from aab_installer.app import create_app
from aab_installer.descriptor import parse

from .conftest import (ECHO_DESCRIPTOR, ECHO_MANIFEST, SOURCE, TOKEN, RepoBuilder, echo_package,
                       local_git, make_settings, run_job)


def env_values(project) -> dict[str, str]:
    out = {}
    for line in (project / ".env").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            k, _, v = line.partition("=")
            out[k] = v
    return out


def secrets_of(project) -> list[str]:
    return [v for k, v in env_values(project).items() if v and (
        k.startswith(("PLUGIN_TOKEN_", "PLUGIN_SECRETS_KEY_")) or k in (
            "INSTALLER_TOKEN", "BROKER_SECRETS_KEY", "SETUP_TOKEN", "DECISION_SIGNING_KEY"))]


def install(client, echo_repo, ref="v0.1.0", commit=None):
    return run_job(client, "/install", {"source": SOURCE, "ref": ref,
                                        "commit": commit or echo_repo.v1})


# ---- the token guard -------------------------------------------------------------------

@pytest.mark.parametrize("method,path", [("POST", "/inspect"), ("POST", "/install"),
                                         ("POST", "/upgrade"), ("POST", "/remove"),
                                         ("GET", "/jobs/" + "0" * 32), ("GET", "/installed")])
def test_every_route_but_health_needs_the_token(app, method, path):
    anon = TestClient(app, raise_server_exceptions=False)
    r = anon.request(method, path, json={})
    assert r.status_code == 401 and r.json() == {"error": "unauthorized",
                                                 "code": "unauthorized"}
    wrong = TestClient(app, headers={"X-Installer-Token": TOKEN + "x"},
                       raise_server_exceptions=False)
    assert wrong.request(method, path, json={}).status_code == 401
    assert r.headers.get("x-request-id")


def test_health_is_tokenless_liveness(app):
    anon = TestClient(app)
    assert anon.get("/health").json() == {"ok": True}
    assert anon.head("/health").status_code == 200


def test_an_empty_token_refuses_to_boot(project, remote, fake_docker):
    for token in ("", "   "):
        with pytest.raises(RuntimeError, match="INSTALLER_TOKEN"):
            create_app(make_settings(project, token=token), git=local_git(remote),
                       runner=fake_docker)


def test_a_relative_home_refuses_to_boot(remote, fake_docker, tmp_path):
    from aab_installer.config import Settings
    with pytest.raises(RuntimeError, match="absolute"):
        create_app(Settings(token=TOKEN, allowed_sources=(), home=tmp_path.relative_to(
            tmp_path.anchor)), git=local_git(remote), runner=fake_docker)


def test_unknown_body_fields_are_refused(client, echo_repo):
    r = client.post("/inspect", json={"source": SOURCE, "ref": "v0.1.0", "url": "x"})
    assert r.status_code == 400 and r.json()["code"] == "bad_request"


# ---- allowlist and refs ---------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/inspect", "/install"])
def test_a_source_outside_the_allowlist_is_refused(client, echo_repo, path):
    other = RepoBuilder(echo_repo.builder.bare.parents[2], "github.com/evil/plugin")
    echo_package(other).commit("x", tag="v0.1.0")
    other.publish()
    body = {"source": "github.com/evil/plugin", "ref": "v0.1.0"}
    if path == "/install":
        body["commit"] = echo_repo.v1
    r = client.post(path, json=body)
    assert r.status_code == 403 and r.json()["code"] == "source_not_allowed"


def test_an_empty_allowlist_refuses_everything(project, remote, fake_docker, echo_repo):
    app = create_app(make_settings(project, allowed=()), git=local_git(remote),
                     runner=fake_docker)
    c = TestClient(app, headers={"X-Installer-Token": TOKEN})
    r = c.post("/inspect", json={"source": SOURCE, "ref": "v0.1.0"})
    assert r.status_code == 403
    assert fake_docker.calls == []


@pytest.mark.parametrize("ref", ["main", "v1.0", "1.0.0", "v1.0.0-rc1", "abc123", "HEAD",
                                 "A" * 40, "v1.0.0;rm -rf /", "--upload-pack=x"])
def test_refs_other_than_a_tag_or_a_full_commit_are_refused(client, echo_repo, ref):
    r = client.post("/inspect", json={"source": SOURCE, "ref": ref})
    assert r.status_code == 400 and "ref must be" in r.json()["error"]


@pytest.mark.parametrize("source", ["file:///etc", "github.com/acme", "ssh://github.com/a/b",
                                    "https://user:pw@github.com/acme/x", "github.com/acme/../x",
                                    "github.com/acme/x?y=1", "-oProxyCommand=x/a/b",
                                    "localhost/acme/x", "github.com/acme/x/y/z/w"])
def test_malformed_sources_are_refused(client, source):
    r = client.post("/inspect", json={"source": source, "ref": "v0.1.0"})
    assert r.status_code == 400 and r.json()["code"] == "bad_request"


def test_a_branch_named_like_a_tag_is_refused(client, echo_repo):
    r = client.post("/inspect", json={"source": SOURCE, "ref": "v9.9.9"})
    assert r.status_code == 400 and "not a tag" in r.json()["error"]


def test_an_unknown_tag_is_a_clone_failure(client, echo_repo):
    r = client.post("/inspect", json={"source": SOURCE, "ref": "v7.7.7"})
    assert r.status_code == 502 and r.json()["code"] == "clone_failed"


# ---- inspect -------------------------------------------------------------------------------

def test_inspect_returns_the_descriptor_manifests_and_commit(client, project, echo_repo):
    r = client.post("/inspect", json={"source": "https://github.com/acme/aab-plugin-echo.git",
                                      "ref": "v0.1.0"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == SOURCE and body["ref"] == "v0.1.0"
    assert body["commit"] == echo_repo.v1
    assert body["descriptor"] == yaml.safe_load(ECHO_DESCRIPTOR) | {
        "build": {"dockerfile": "Dockerfile"}, "environment": {}}
    [m] = body["manifests"]
    assert m["plugin"] == "echo" and m["path"] == "aab_plugin_echo/manifest.yaml"
    assert m["version"] == "0.1.0"
    assert m["text"] == ECHO_MANIFEST.read_text(encoding="utf-8")
    assert body["installed"] is None
    # The temporary clone is gone, and nothing was installed or run.
    assert list((project / "plugins.d" / "_installer" / "tmp").iterdir()) == []
    assert not (project / "plugins.d" / "echo").exists()


def test_inspect_by_commit(client, echo_repo):
    r = client.post("/inspect", json={"source": SOURCE, "ref": echo_repo.v2})
    assert r.status_code == 200, r.text
    assert r.json()["commit"] == echo_repo.v2 and r.json()["manifests"][0]["version"] == "0.2.0"


def test_a_symlinked_manifest_never_reads_the_host(client, project, remote):
    b = RepoBuilder(remote, "github.com/acme/sneaky")
    echo_package(b)
    (b.work / "aab_plugin_echo" / "manifest.yaml").unlink()
    b.symlink("aab_plugin_echo/manifest.yaml", "../../../../aab/.env").commit("x", tag="v0.1.0")
    b.publish()
    r = client.post("/inspect", json={"source": "github.com/acme/sneaky", "ref": "v0.1.0"})
    assert r.status_code == 422 and r.json()["code"] == "invalid_package"
    # Checked out as a plain file holding the link text, so it is not even
    # YAML of the right shape; a real symlink would be refused by name.
    assert "must be a mapping" in r.json()["error"] or "symlink" in r.json()["error"]
    for secret in secrets_of(project):
        assert secret not in r.text


@pytest.mark.parametrize("descriptor,fragment", [
    (ECHO_DESCRIPTOR.replace("service: echo", "service: installer"), "already uses"),
    (ECHO_DESCRIPTOR + "ports: ['8090:8090']\n", "ports"),
    (ECHO_DESCRIPTOR.replace("plugins: [echo]", "plugins: [other]"), "declares id"),
])
def test_an_invalid_package_is_422(client, remote, descriptor, fragment):
    b = RepoBuilder(remote, "github.com/acme/bad")
    echo_package(b).write("aab-plugin.yaml", descriptor).commit("x", tag="v0.1.0")
    b.publish()
    r = client.post("/inspect", json={"source": "github.com/acme/bad", "ref": "v0.1.0"})
    assert r.status_code == 422 and fragment in r.json()["error"]


# ---- install ------------------------------------------------------------------------------

def test_install_runs_the_expected_commands_and_leaves_the_expected_files(client, project,
                                                                          fake_docker,
                                                                          echo_repo):
    job = install(client, echo_repo)
    assert job["state"] == "done", job
    assert (job["kind"], job["service"], job["source"], job["ref"], job["commit"]) == (
        "install", "echo", SOURCE, "v0.1.0", echo_repo.v1)
    assert fake_docker.commands() == [["compose", "up", "-d", "--build", "plugin-echo"],
                                      ["compose", "up", "-d", "broker"]]
    # Every compose run is against the checkout, with the new overlay in the set.
    for argv in fake_docker.calls:
        assert argv[:4] == ["docker", "compose", "--project-directory", str(project)]
        assert "plugins.d/echo/compose.yml" in argv
    svc = project / "plugins.d" / "echo"
    assert (svc / "src" / "aab-plugin.yaml").read_text(encoding="utf-8") == ECHO_DESCRIPTOR
    assert (svc / "compose.yml").read_text(encoding="utf-8") == overlay.render(
        parse(ECHO_DESCRIPTOR), "plugins.d/echo")
    record = json.loads((svc / "install.json").read_text(encoding="utf-8"))
    assert (record["service"], record["source"], record["ref"], record["commit"]) == (
        "echo", SOURCE, "v0.1.0", echo_repo.v1)
    assert record["plugins"] == ["echo"] and record["volumes"] == ["echo_secrets", "echo_data"]
    assert envfile.has_value(project / ".env", "PLUGIN_TOKEN_ECHO")
    assert envfile.has_value(project / ".env", "PLUGIN_SECRETS_KEY_ECHO")
    [item] = client.get("/installed").json()["items"]
    assert item["service"] == "echo" and item["commit"] == echo_repo.v1
    # The job's log is for the owner: steps and commands, never a secret.
    text = json.dumps(job)
    assert "docker compose" in text and "PLUGIN_TOKEN_ECHO" in text
    for secret in secrets_of(project):
        assert secret not in text


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_the_overlay_and_record_are_readable_by_the_deploying_user(client, project, echo_repo):
    install(client, echo_repo)
    for name in ("compose.yml", "install.json"):
        mode = stat.S_IMODE(os.stat(project / "plugins.d" / "echo" / name).st_mode)
        assert mode == 0o644, name


def test_install_refuses_a_commit_other_than_the_reviewed_one(client, project, fake_docker,
                                                              echo_repo):
    job = install(client, echo_repo, commit=echo_repo.v2)       # v0.1.0 is v1
    assert job["state"] == "failed" and "not the reviewed" in job["error"]
    assert fake_docker.calls == [] and not (project / "plugins.d" / "echo").exists()


def test_a_failed_build_rolls_everything_back(client, project, fake_docker, echo_repo):
    fake_docker.fail["--build plugin-echo"] = 1
    job = install(client, echo_repo)
    assert job["state"] == "failed" and "plugin-echo failed (exit 1)" in job["error"]
    assert fake_docker.commands() == [["compose", "up", "-d", "--build", "plugin-echo"],
                                      ["compose", "rm", "-s", "-f", "plugin-echo"],
                                      ["compose", "up", "-d", "broker"]]
    # The broken overlay is out of the file set before the broker is touched.
    assert "plugins.d/echo/compose.yml" not in fake_docker.calls[-1]
    assert not (project / "plugins.d" / "echo").exists()
    assert client.get("/installed").json() == {"items": []}
    assert any("simulated failure" in line for line in job["log"])


def test_a_second_install_of_a_service_is_refused(client, project, echo_repo):
    assert install(client, echo_repo)["state"] == "done"
    before = (project / "plugins.d" / "echo" / "install.json").read_bytes()
    job = install(client, echo_repo, ref="v0.2.0", commit=echo_repo.v2)
    assert job["state"] == "failed" and "already installed" in job["error"]
    assert (project / "plugins.d" / "echo" / "install.json").read_bytes() == before


def test_one_job_at_a_time(client, fake_docker, echo_repo):
    fake_docker.hold("--build plugin-echo")
    r = client.post("/install", json={"source": SOURCE, "ref": "v0.1.0", "commit": echo_repo.v1})
    assert r.status_code == 202
    assert fake_docker.entered.wait(10)
    second = client.post("/remove", json={"service": "echo"})
    assert second.status_code in (404, 409)                 # not installed yet, or busy
    busy = client.post("/install", json={"source": SOURCE, "ref": "v0.2.0",
                                         "commit": echo_repo.v2})
    assert busy.status_code == 409 and busy.json()["code"] == "busy"
    running = client.get(f"/jobs/{r.json()['id']}").json()
    assert running["state"] == "running"
    fake_docker.release()
    assert client.app.state.store.wait_idle()
    assert client.get(f"/jobs/{r.json()['id']}").json()["state"] == "done"


def test_unknown_jobs_are_404(client):
    for job_id in ("0" * 32, "nope", "../../etc/passwd"):
        r = client.get(f"/jobs/{job_id}")
        assert r.status_code == 404


# ---- upgrade ------------------------------------------------------------------------------

def test_upgrade_moves_to_the_new_commit(client, project, fake_docker, echo_repo):
    install(client, echo_repo)
    installed_at = client.get("/installed").json()["items"][0]["installed_at"]
    fake_docker.calls.clear()
    job = run_job(client, "/upgrade", {"service": "echo", "source": SOURCE, "ref": "v0.2.0",
                                       "commit": echo_repo.v2})
    assert job["state"] == "done", job
    assert fake_docker.commands() == [["compose", "up", "-d", "--build", "plugin-echo"],
                                      ["compose", "up", "-d", "broker"]]
    [item] = client.get("/installed").json()["items"]
    assert (item["ref"], item["commit"], item["installed_at"]) == (
        "v0.2.0", echo_repo.v2, installed_at)
    manifest = project / "plugins.d" / "echo" / "src" / "aab_plugin_echo" / "manifest.yaml"
    assert "version: 0.2.0" in manifest.read_text(encoding="utf-8")


def test_a_failed_upgrade_restores_the_previous_files(client, project, fake_docker, echo_repo):
    install(client, echo_repo)
    svc = project / "plugins.d" / "echo"
    before = {p: (svc / p).read_bytes() for p in ("compose.yml", "install.json",
                                                  "src/aab_plugin_echo/manifest.yaml")}
    fake_docker.fail["--build plugin-echo"] = 2
    job = run_job(client, "/upgrade", {"service": "echo", "source": SOURCE, "ref": "v0.2.0",
                                       "commit": echo_repo.v2})
    assert job["state"] == "failed"
    assert {p: (svc / p).read_bytes() for p in before} == before


def test_upgrade_needs_an_installed_service_from_the_same_source(client, echo_repo):
    body = {"service": "echo", "source": SOURCE, "ref": "v0.2.0", "commit": echo_repo.v2}
    r = client.post("/upgrade", json=body)
    assert r.status_code == 404 and r.json()["code"] == "not_installed"
    install(client, echo_repo)
    r = client.post("/upgrade", json={**body, "source": "github.com/acme/other"})
    assert r.status_code == 409 and r.json()["code"] == "source_changed"
    r = client.post("/upgrade", json={**body, "service": "Bad"})
    assert r.status_code == 400


def test_upgrade_refuses_a_package_for_another_service(client, project, remote, echo_repo):
    install(client, echo_repo)
    b = echo_repo.builder
    commit = echo_package(b, "0.3.0").write(
        "aab-plugin.yaml", ECHO_DESCRIPTOR.replace("service: echo", "service: echox")
        .replace("echo_data", "echox_data")).commit("rename", tag="v0.3.0")
    b.publish()
    job = run_job(client, "/upgrade", {"service": "echo", "source": SOURCE, "ref": "v0.3.0",
                                       "commit": commit})
    assert job["state"] == "failed" and "not echo" in job["error"]
    assert not (project / "plugins.d" / "echox").exists()


# ---- remove -------------------------------------------------------------------------------

def test_remove_keeps_volumes_and_retires_the_secrets(client, project, fake_docker, echo_repo):
    install(client, echo_repo)
    token = env_values(project)["PLUGIN_TOKEN_ECHO"]
    fake_docker.calls.clear()
    job = run_job(client, "/remove", {"service": "echo"})
    assert job["state"] == "done", job
    assert (job["kind"], job["service"], job["purge"]) == ("remove", "echo", False)
    assert fake_docker.commands() == [["compose", "rm", "-s", "-f", "plugin-echo"],
                                      ["compose", "up", "-d", "broker"],
                                      ["docker", "network", "rm", "aab_net_echo"]]
    # Stopped while its overlay was in the set; the broker recreated without it.
    assert "plugins.d/echo/compose.yml" in fake_docker.calls[0]
    assert "plugins.d/echo/compose.yml" not in fake_docker.calls[1]
    assert not (project / "plugins.d" / "echo").exists()
    assert not envfile.has_value(project / ".env", "PLUGIN_TOKEN_ECHO")
    assert f"#aab-retired# PLUGIN_TOKEN_ECHO={token}" in (project / ".env").read_text()
    assert client.get("/installed").json() == {"items": []}
    # A reinstall gets the same secrets back (the kept volume still decrypts).
    assert install(client, echo_repo)["state"] == "done"
    assert env_values(project)["PLUGIN_TOKEN_ECHO"] == token


def test_remove_with_purge_deletes_volumes_and_secrets(client, project, fake_docker, echo_repo):
    install(client, echo_repo)
    fake_docker.calls.clear()
    job = run_job(client, "/remove", {"service": "echo", "purge": True})
    assert job["state"] == "done", job
    assert fake_docker.commands()[3:] == [["docker", "volume", "rm", "aab_echo_secrets"],
                                          ["docker", "volume", "rm", "aab_echo_data"]]
    assert "PLUGIN_TOKEN_ECHO" not in (project / ".env").read_text()


def test_a_failed_stop_keeps_the_service_in_place(client, project, fake_docker, echo_repo):
    install(client, echo_repo)
    fake_docker.fail["rm -s -f plugin-echo"] = 1
    job = run_job(client, "/remove", {"service": "echo"})
    assert job["state"] == "failed"
    assert (project / "plugins.d" / "echo" / "compose.yml").exists()
    assert envfile.has_value(project / ".env", "PLUGIN_TOKEN_ECHO")


def test_remove_needs_an_installed_valid_service(client):
    assert client.post("/remove", json={"service": "echo"}).status_code == 404
    for bad in ("../echo", "Echo", "installer", ""):
        assert client.post("/remove", json={"service": bad}).status_code == 400


# ---- persistence ---------------------------------------------------------------------------

def test_jobs_survive_an_installer_restart(project, remote, fake_docker, client, echo_repo):
    job = install(client, echo_repo)
    again = create_app(make_settings(project), git=local_git(remote), runner=fake_docker)
    c = TestClient(again, headers={"X-Installer-Token": TOKEN})
    assert c.get(f"/jobs/{job['id']}").json() == job
    assert [i["service"] for i in c.get("/installed").json()["items"]] == ["echo"]


def test_the_job_log_never_carries_the_installer_token(client, project, echo_repo, caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    install(client, echo_repo)
    client.post("/inspect", json={"source": SOURCE, "ref": "v0.1.0"})
    TestClient(client.app, raise_server_exceptions=False).get("/installed")   # a refusal line
    assert TOKEN not in caplog.text
    for secret in secrets_of(project):
        assert secret not in caplog.text


# ---- the GitHub token a request carries --------------------------------------------------

# A fake value with no known token prefix, of a shape no redaction row knows.
PLANTED = "Planted-0123456789-xyzTOKEN"


def test_a_requests_git_token_reaches_git_only_and_is_kept_nowhere(
        project, remote, fake_docker, echo_repo, caplog):
    """The broker's GitHub token, sent with inspect, install and upgrade:
    each clone gets it through the askpass environment only (no argv, no
    other command); even if a command printed it, the job log masks it; and
    no response, job record, install.json, rendered file, .env or log line
    ever carries it. Once a job has run, not even memory holds it."""
    import logging

    from aab_installer.compose import Result
    from aab_installer.git import _run

    caplog.set_level(logging.DEBUG)
    seen = []

    def recording_git(argv, cwd, env, timeout):
        seen.append((list(argv), dict(env)))
        return _run(argv, cwd, env, timeout)

    leak = {"on": True}         # only while a job that carried the token runs

    def leaky_docker(argv, cwd, timeout):
        result = fake_docker(argv, cwd, timeout)
        if list(argv)[:1] != ["docker"] or not leak["on"]:
            return result                       # the compose file list stays parseable
        return Result(result.returncode, f"{result.output}\nusing {PLANTED}")

    app = create_app(make_settings(project), git=local_git(remote, runner=recording_git),
                     runner=leaky_docker)
    c = TestClient(app, headers={"X-Installer-Token": TOKEN}, raise_server_exceptions=False)
    inspected = c.post("/inspect", json={"source": SOURCE, "ref": "v0.1.0",
                                         "git_token": PLANTED})
    assert inspected.status_code == 200, inspected.text
    job = run_job(c, "/install", {"source": SOURCE, "ref": "v0.1.0", "commit": echo_repo.v1,
                                  "git_token": PLANTED})
    assert job["state"] == "done", job
    assert any("using <redacted>" in line for line in job["log"])
    upgraded = run_job(c, "/upgrade", {"service": "echo", "source": SOURCE, "ref": "v0.2.0",
                                       "commit": echo_repo.v2, "git_token": PLANTED})
    assert upgraded["state"] == "done", upgraded
    assert any("using <redacted>" in line for line in upgraded["log"])
    assert app.state.store._work == {}                 # the closures holding it are gone
    leak["on"] = False
    # Only the network command of each clone carried it, through askpass.
    carried = [argv[7] for argv, env in seen if env.get("AAB_GIT_TOKEN") == PLANTED]
    assert carried == ["clone", "clone", "clone"]
    for argv, env in seen:
        assert not any(PLANTED in a for a in argv)
        if "AAB_GIT_TOKEN" in env:
            assert env["GIT_ASKPASS"].endswith("git-askpass")
    # An inspect without it is anonymous: nothing remembered the last one.
    seen.clear()
    assert c.post("/inspect", json={"source": SOURCE, "ref": "v0.1.0"}).status_code == 200
    assert seen and not any("AAB_GIT_TOKEN" in env for _, env in seen)
    removed = run_job(c, "/remove", {"service": "echo", "purge": False})
    for text in (inspected.text, json.dumps(job), json.dumps(upgraded), json.dumps(removed),
                 c.get("/installed").text, caplog.text):
        assert PLANTED not in text
    # Nothing on disk: the checkout, plugins.d (jobs, install.json, the
    # overlay), .env, the askpass script's directory, the remotes.
    for f in remote.parent.rglob("*"):
        if f.is_file():
            assert PLANTED.encode() not in f.read_bytes(), f
    assert "git_auth=per_request" in caplog.text


@pytest.mark.parametrize("bad", ["", "short", "x" * 256, "has space inside it 0123",
                                 "newline\n0123456789abcdef", "trailing-newline-0123456789\n",
                                 "non-ascii-é-0123456789", 12345])
def test_a_malformed_git_token_is_a_400_that_does_not_echo_it(client, fake_docker, echo_repo,
                                                              bad):
    install(client, echo_repo)                       # an installed service, for /upgrade
    fake_docker.calls.clear()
    jobs_before = len(client.app.state.store.all())
    bodies = {"/inspect": {"source": SOURCE, "ref": "v0.1.0"},
              "/install": {"source": SOURCE, "ref": "v0.1.0", "commit": echo_repo.v1},
              "/upgrade": {"service": "echo", "source": SOURCE, "ref": "v0.2.0",
                           "commit": echo_repo.v2}}
    for path, body in bodies.items():
        r = client.post(path, json={**body, "git_token": bad})
        assert r.status_code == 400 and r.json()["code"] == "bad_request", (path, r.text)
        if isinstance(bad, str) and bad:
            assert bad not in r.text
    assert len(client.app.state.store.all()) == jobs_before and fake_docker.calls == []


def test_a_git_token_is_not_accepted_where_no_clone_happens(client, echo_repo):
    install(client, echo_repo)
    r = client.post("/remove", json={"service": "echo", "git_token": PLANTED})
    assert r.status_code == 400 and PLANTED not in r.text
