"""Source normalization, the allowlist (fail closed, `*` is one segment), the
ref rules, and the git client's isolation (no global config, only the
allowed transport, symlinks checked out as plain files)."""

import os

import pytest

from aab_installer.git import Git, GitError, allowed, normalize_source, parse_allowlist, ref_kind

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
