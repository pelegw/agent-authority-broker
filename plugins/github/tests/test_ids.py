"""Canonical ids: one spelling per GitHub object, so hidden lists and grants
(exact-string comparisons) cannot be sidestepped by a second name."""

import pytest

from aab_plugin_runtime import AdapterError
from aab_plugin_github.ids import (check_branch, check_path, check_ref, check_sha,
                                   encode_path, encode_ref, normalize_owner, normalize_repo)


def test_repo_is_lowercased_in_both_parts():
    # GitHub resolves owner AND name case-insensitively: lowercasing only the
    # owner would leave octo/Hello as a second name for a hidden octo/hello.
    assert normalize_repo("Octo/Hello") == "octo/hello"
    assert normalize_repo("  octo/hello.world_2-x ") == "octo/hello.world_2-x"


@pytest.mark.parametrize("bad", [
    "octo", "octo/", "/hello", "octo/hello/extra", "octo//hello", "-octo/hello",
    "octo/hello.git",                  # GitHub strips .git: another alias
    "octo/.", "octo/..", "oc to/hello", "octo/hel lo", "octo/hello?x", "octo/hello#x",
    "octo/%68ello", "a" * 40 + "/x", "octo/" + "x" * 101, "",
    "octo/K",                     # Kelvin sign: lower() would fold it to "k"
    "осto/hello",            # Cyrillic lookalikes
    None, 5, ["octo/hello"],
])
def test_bad_repo_ids_are_400(bad):
    with pytest.raises(AdapterError) as e:
        normalize_repo(bad)
    assert e.value.status == 400


def test_owner():
    assert normalize_owner("Octo") == "octo"
    for bad in (None, "", "-x", "a/b", "K"):
        with pytest.raises(AdapterError):
            normalize_owner(bad)


def test_branch_names_are_exact_and_case_sensitive():
    assert check_branch("Feature/X") == "Feature/X"
    assert check_branch("agent/fix-typo") == "agent/fix-typo"


@pytest.mark.parametrize("bad", [
    "", "a..b", "a b", "a~b", "a^b", "a:b", "a?b", "feat/*", "a[b", "a\\b", "a@{b", "@",
    "/a", "a/", "a//b", "a.", ".a", "a/.b", "a.lock", "a/b.lock", "-a", "a\x00b", "a\x7fb",
    "refs/heads/main", "HEAD", "x" * 251, None, 3,
])
def test_bad_branch_names_are_400(bad):
    with pytest.raises(AdapterError) as e:
        check_branch(bad)
    assert e.value.status == 400


def test_refs_may_be_tags_shas_and_full_refs():
    assert check_ref("v1.2.3") == "v1.2.3"
    assert check_ref("refs/tags/v1") == "refs/tags/v1"
    assert check_ref("a" * 40) == "a" * 40


@pytest.mark.parametrize("bad", ["", "/a", "a/", "a//b", "../x", "a/../b", "a/./b", ".",
                                 "a\\b", "a\x00b", "a\nb", "x" * 1025, None])
def test_bad_paths_are_400(bad):
    with pytest.raises(AdapterError) as e:
        check_path(bad)
    assert e.value.status == 400


def test_paths_are_encoded_per_segment():
    # '?', '#' and '%' can never turn into a query, a fragment or a second
    # decoding: each segment is percent-encoded, '/' stays a separator.
    assert encode_path("docs/a b?c#d%e.md") == "docs/a%20b%3Fc%23d%25e.md"
    assert encode_ref("feat/x%y") == "feat/x%25y"


def test_sha():
    assert check_sha("0" * 40) == "0" * 40 and check_sha("a" * 64)
    for bad in ("0" * 39, "g" * 40, "A" * 40, None):
        with pytest.raises(AdapterError):
            check_sha(bad)
