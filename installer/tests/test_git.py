"""Source normalization, the allowlist (fail closed, `*` is one segment), the
ref rules, the git client's isolation (no global config, only the allowed
transport, symlinks checked out as plain files), and the private-repository
credential: INSTALLER_GIT_TOKEN reaches git only through GIT_ASKPASS, only on
the commands that talk to github.com, and never in an argument or a URL."""

import os
import shutil
import stat
import subprocess

import pytest

from aab_installer.git import (ASKPASS_FILE, ASKPASS_SCRIPT, Git, GitError, allowed,
                               normalize_source, parse_allowlist, ref_kind, write_askpass)

from .conftest import SOURCE, RepoBuilder, echo_package


@pytest.mark.parametrize("raw,expected", [
    ("github.com/acme/repo", "github.com/acme/repo"),
    ("https://github.com/acme/repo", "github.com/acme/repo"),
    ("https://github.com/acme/repo.git", "github.com/acme/repo"),
    ("github.com/acme/repo/", "github.com/acme/repo"),
    ("GitHub.com/Acme/Repo", "github.com/Acme/Repo"),         # host folded, path kept
    ("gitlab.example.org/group/sub/repo", "gitlab.example.org/group/sub/repo"),
    ("  github.com/acme/re.po_x-1  ", "github.com/acme/re.po_x-1"),
])
def test_sources_normalize(raw, expected):
    assert normalize_source(raw) == expected


@pytest.mark.parametrize("raw", [
    None, 7, "", "github.com", "github.com/acme", "http://github.com/acme/repo",
    "ssh://git@github.com/acme/repo", "git@github.com:acme/repo", "file:///etc/passwd",
    "https://user:pw@github.com/acme/repo", "github.com/acme/repo?x=1", "github.com/acme/repo#x",
    "github.com/acme/../repo", "github.com/./repo", "github.com/acme/repo.",
    "github.com/acme/-repo", "localhost/acme/repo", "github.com/a/b/c/d/e",
    "github.com:22/acme/repo", "github.com/acme/re po", "github.com//repo",
])
def test_malformed_sources_are_refused(raw):
    with pytest.raises(GitError) as e:
        normalize_source(raw)
    assert e.value.status == 400


def test_the_allowlist_is_parsed_strictly():
    assert parse_allowlist("") == ()
    assert parse_allowlist(" , ") == ()
    assert parse_allowlist("github.com/pelegw/*, GitHub.com/acme/repo\ngitlab.com/g/s/*") == (
        "github.com/pelegw/*", "github.com/acme/repo", "gitlab.com/g/s/*")
    # Malformed entries can only ever match nothing: dropped.
    assert parse_allowlist("*, */*/*, github.com/*, github.com/**/x, https://github.com/a/b") == ()


@pytest.mark.parametrize("source,patterns,ok", [
    ("github.com/pelegw/aab-plugin-finance", ("github.com/pelegw/*",), True),
    ("github.com/pelegw/aab-plugin-finance", ("github.com/pelegw/aab-plugin-finance",), True),
    ("github.com/pelegw/x", (), False),                              # empty: nothing
    ("github.com/pelegwx/x", ("github.com/pelegw/*",), False),
    ("github.com/other/x", ("github.com/pelegw/*",), False),
    ("github.com/pelegw/x/y", ("github.com/pelegw/*",), False),      # * is one segment
    ("gitlab.com/pelegw/x", ("github.com/pelegw/*",), False),
    ("github.com/PelegW/x", ("github.com/pelegw/*",), False),        # path is exact
    ("evil.com/pelegw/x", ("github.com/pelegw/*",), False),
])
def test_allowlist_matching(source, patterns, ok):
    assert allowed(source, patterns) is ok


@pytest.mark.parametrize("ref,kind", [("v0.1.0", "tag"), ("v10.20.30", "tag"),
                                      ("0" * 40, "commit"), ("a1" * 20, "commit")])
def test_refs_accepted(ref, kind):
    assert ref_kind(ref) == kind


@pytest.mark.parametrize("ref", [None, "", "main", "v1", "v1.2", "1.2.3", "v1.2.3.4", "V1.2.3",
                                 "v1.2.3-rc1", "0" * 39, "0" * 41, "G" * 40, "HEAD~1",
                                 "--upload-pack=touch /tmp/x", "refs/tags/v1.2.3"])
def test_refs_refused(ref):
    with pytest.raises(GitError) as e:
        ref_kind(ref)
    assert e.value.status == 400


def test_git_runs_isolated(tmp_path):
    seen = {}

    def runner(argv, cwd, env, timeout):
        seen["argv"], seen["env"] = list(argv), dict(env)
        raise OSError("no git here")

    with pytest.raises(GitError) as e:
        Git(runner=runner).fetch(SOURCE, "v0.1.0", tmp_path / "dest")
    assert e.value.status == 503
    assert seen["argv"][:7] == ["git", "-c", "core.symlinks=false", "-c",
                                "advice.detachedHead=false", "-c",
                                "core.hooksPath=" + os.devnull]
    assert "https://github.com/acme/aab-plugin-echo.git" in seen["argv"]
    env = seen["env"]
    assert env["GIT_ALLOW_PROTOCOL"] == "https" and env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1" and env["GIT_CONFIG_GLOBAL"] == os.devnull
    # Nothing else of the process environment (no token reaches git).
    assert not {k for k in env if k.startswith("INSTALLER_")}
    assert not (tmp_path / "dest").exists()


def test_only_the_allowed_transport_is_used(remote, tmp_path):
    b = RepoBuilder(remote)
    echo_package(b).commit("x", tag="v0.1.0")
    b.publish()
    https_only = Git(url_for=lambda s: (remote / f"{s}.git").as_uri())     # file:// refused
    with pytest.raises(GitError) as e:
        https_only.fetch(SOURCE, "v0.1.0", tmp_path / "dest")
    assert e.value.status == 502 and not (tmp_path / "dest").exists()


def test_symlinks_are_checked_out_as_plain_files(remote, tmp_path):
    b = RepoBuilder(remote)
    echo_package(b).symlink("link", "../../../../etc/passwd").commit("x", tag="v0.1.0")
    b.publish()
    git = Git(url_for=lambda s: (remote / f"{s}.git").as_uri(), protocols=("file",))
    git.fetch(SOURCE, "v0.1.0", tmp_path / "dest")
    link = tmp_path / "dest" / "link"
    assert not os.path.islink(link)
    assert link.read_text() == "../../../../etc/passwd"


def test_a_commit_ref_must_come_back_as_that_commit(echo_repo, remote, tmp_path):
    git = Git(url_for=lambda s: (remote / f"{s}.git").as_uri(), protocols=("file",))
    assert git.fetch(SOURCE, echo_repo.v1, tmp_path / "a") == echo_repo.v1
    with pytest.raises(GitError):
        git.fetch(SOURCE, "f" * 40, tmp_path / "b")                    # not in the repo
    assert not (tmp_path / "b").exists()


# ---- private repositories: INSTALLER_GIT_TOKEN through GIT_ASKPASS -----------------------

GIT_TOKEN = "github_pat_11TESTONLY0abcdefghijKLMNOPQRSTUVWXYZ0123456789"
SH = shutil.which("sh")


def recording(calls, fail_from=0):
    """A git runner that records (argv, env) and fails every command after the
    first `fail_from` ones, with the token in its stderr as a careless git
    might print it."""
    def runner(argv, cwd, env, timeout):
        calls.append((list(argv), dict(env)))
        code = 0 if len(calls) <= fail_from else 128
        return subprocess.CompletedProcess(argv, code, "", f"fatal: auth {GIT_TOKEN} refused")
    return runner


def test_a_tag_clone_gets_the_askpass_env_when_a_token_is_configured(tmp_path):
    calls = []
    git = Git(runner=recording(calls), token=GIT_TOKEN, askpass_dir=tmp_path / "state")
    with pytest.raises(GitError) as e:
        git.fetch(SOURCE, "v0.1.0", tmp_path / "dest")
    [(argv, env)] = calls
    assert argv[7] == "clone"
    assert env["GIT_ASKPASS"] == str(tmp_path / "state" / ASKPASS_FILE)
    assert env["AAB_GIT_TOKEN"] == GIT_TOKEN and env["AAB_GIT_HOST"] == "github.com"
    assert env["GIT_TERMINAL_PROMPT"] == "0"                 # askpass or nothing
    script = tmp_path / "state" / ASKPASS_FILE
    assert script.read_bytes() == ASKPASS_SCRIPT.encode("ascii")
    assert GIT_TOKEN not in script.read_text()
    # Never in an argument or the URL; git's words never reach the error.
    assert not any(GIT_TOKEN in a for a in argv)
    assert "https://github.com/acme/aab-plugin-echo.git" in argv and not any("@" in a for a in argv)
    assert GIT_TOKEN not in e.value.message and GIT_TOKEN not in str(e.value)


def test_a_commit_fetch_gets_it_on_the_fetch_only(tmp_path):
    calls = []
    git = Git(runner=recording(calls, fail_from=1), token=GIT_TOKEN,
              askpass_dir=tmp_path / "state")
    with pytest.raises(GitError):
        git.fetch(SOURCE, "a" * 40, tmp_path / "dest")
    (init_argv, init_env), (fetch_argv, fetch_env) = calls
    assert init_argv[7] == "init" and init_env["GIT_ASKPASS"] == ""
    assert "AAB_GIT_TOKEN" not in init_env
    assert fetch_argv[7] == "fetch" and fetch_env["AAB_GIT_TOKEN"] == GIT_TOKEN
    assert not (tmp_path / "dest").exists()


def test_without_a_token_git_is_anonymous(tmp_path):
    calls = []
    with pytest.raises(GitError):
        Git(runner=recording(calls)).fetch(SOURCE, "v0.1.0", tmp_path / "dest")
    [(_, env)] = calls
    assert env["GIT_ASKPASS"] == "" and env["SSH_ASKPASS"] == ""
    assert not {k for k in env if k.startswith("AAB_GIT_")}
    assert not Git().authenticated


def test_the_token_is_offered_to_github_com_sources_only(tmp_path):
    calls = []
    git = Git(runner=recording(calls), token=GIT_TOKEN, askpass_dir=tmp_path / "state")
    assert git.authenticated
    with pytest.raises(GitError):
        git.fetch("gitlab.com/acme/aab-plugin-echo", "v0.1.0", tmp_path / "dest")
    [(_, env)] = calls
    assert env["GIT_ASKPASS"] == "" and "AAB_GIT_TOKEN" not in env
    assert not (tmp_path / "state" / ASKPASS_FILE).exists()


def test_a_token_needs_a_place_for_its_script():
    with pytest.raises(ValueError):
        Git(token=GIT_TOKEN)


def test_a_tampered_script_is_rewritten_before_use(tmp_path):
    path = write_askpass(tmp_path)
    path.write_text("#!/bin/sh\necho stolen > /tmp/x\n")
    assert write_askpass(tmp_path).read_bytes() == ASKPASS_SCRIPT.encode("ascii")
    if os.name == "posix":
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o700


@pytest.mark.skipif(SH is None, reason="no POSIX sh on this machine")
@pytest.mark.parametrize("prompt,answer", [
    ("Username for 'https://github.com': ", "x-access-token"),
    ("Password for 'https://x-access-token@github.com': ", GIT_TOKEN),
    # Another host (a redirect), another user, or a prompt nobody expected:
    # nothing, and a failing exit, so git gives up instead of sending it.
    ("Username for 'https://evil.example': ", None),
    ("Password for 'https://x-access-token@evil.example': ", None),
    ("Password for 'https://x-access-token@github.com.evil.example': ", None),
    ("Password for 'https://someone@github.com': ", None),
    ("Enter passphrase for key '/root/.ssh/id_rsa': ", None),
])
def test_the_askpass_script_answers_github_com_only(tmp_path, prompt, answer):
    script = write_askpass(tmp_path)
    env = {**os.environ, "AAB_GIT_HOST": "github.com", "AAB_GIT_TOKEN": GIT_TOKEN}
    r = subprocess.run([SH, str(script), prompt], capture_output=True, text=True, env=env)
    if answer is None:
        assert r.returncode != 0 and r.stdout == ""
    else:
        assert r.returncode == 0 and r.stdout == answer + "\n"
