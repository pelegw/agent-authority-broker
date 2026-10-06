"""Reading a checked-out package: the descriptor, each manifest (a mapping
whose id is the one listed for it, with a MAJOR.MINOR.PATCH version) and
the Dockerfile; never through a symlink, never outside the checkout, never
beyond a size cap."""

import os

import pytest

from aab_installer.package import MAX_MANIFEST_BYTES, PackageError, read_file, read_package

from .conftest import DOCKERFILE, ECHO_DESCRIPTOR, ECHO_MANIFEST


@pytest.fixture()
def checkout(tmp_path):
    root = tmp_path / "src"
    (root / "aab_plugin_echo").mkdir(parents=True)
    (root / "aab-plugin.yaml").write_bytes(ECHO_DESCRIPTOR.encode())
    (root / "Dockerfile").write_bytes(DOCKERFILE.encode())
    (root / "aab_plugin_echo" / "manifest.yaml").write_bytes(
        ECHO_MANIFEST.read_text(encoding="utf-8").encode())
    return root


def test_a_valid_package_reads(checkout):
    pkg = read_package(checkout)
    assert pkg.descriptor.service == "echo"
    [m] = pkg.manifests
    assert (m["plugin"], m["path"], m["version"]) == ("echo", "aab_plugin_echo/manifest.yaml",
                                                      "0.1.0")
    assert m["text"] == ECHO_MANIFEST.read_text(encoding="utf-8")


@pytest.mark.parametrize("rel,content,fragment", [
    ("aab_plugin_echo/manifest.yaml", "id: other\nversion: 0.1.0\n", "declares id"),
    ("aab_plugin_echo/manifest.yaml", "id: echo\nversion: latest\n", "MAJOR.MINOR.PATCH"),
    ("aab_plugin_echo/manifest.yaml", "- not a mapping\n", "mapping"),
    ("aab_plugin_echo/manifest.yaml", "id: [unclosed", "YAML"),
    ("aab-plugin.yaml", ECHO_DESCRIPTOR.replace("schema: 1", "schema: 2"), "schema"),
])
def test_invalid_contents_are_refused(checkout, rel, content, fragment):
    (checkout / rel).write_text(content)
    with pytest.raises(PackageError) as e:
        read_package(checkout)
    assert fragment in str(e.value)


@pytest.mark.parametrize("missing", ["aab-plugin.yaml", "Dockerfile",
                                     "aab_plugin_echo/manifest.yaml"])
def test_missing_files_are_refused(checkout, missing):
    (checkout / missing).unlink()
    with pytest.raises(PackageError, match="missing"):
        read_package(checkout)


def test_a_directory_is_not_a_file(checkout):
    (checkout / "Dockerfile").unlink()
    (checkout / "Dockerfile").mkdir()
    with pytest.raises(PackageError, match="regular file"):
        read_package(checkout)


def test_size_caps(checkout):
    (checkout / "aab_plugin_echo" / "manifest.yaml").write_bytes(b"#" * (MAX_MANIFEST_BYTES + 1))
    with pytest.raises(PackageError, match="larger"):
        read_package(checkout)


@pytest.mark.parametrize("rel", ["../x", "a/../b", "/etc/passwd", "a//b", "."])
def test_paths_must_stay_plain(checkout, rel):
    with pytest.raises(PackageError):
        read_file(checkout, rel, 100)


def _symlink_or_skip(target, link, directory=False):
    try:
        os.symlink(target, link, target_is_directory=directory)
    except (OSError, NotImplementedError):
        pytest.skip("this system cannot create symlinks")


def test_a_symlinked_file_is_refused(checkout, tmp_path):
    secret = tmp_path / "host.env"
    secret.write_text("PLUGIN_TOKEN_GITHUB=" + "ab" * 32)
    manifest = checkout / "aab_plugin_echo" / "manifest.yaml"
    manifest.unlink()
    _symlink_or_skip(secret, manifest)
    with pytest.raises(PackageError, match="symlink"):
        read_package(checkout)


def test_a_symlinked_directory_is_refused(checkout, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "manifest.yaml").write_text(ECHO_MANIFEST.read_text(encoding="utf-8"))
    for p in (checkout / "aab_plugin_echo").iterdir():
        p.unlink()
    (checkout / "aab_plugin_echo").rmdir()
    _symlink_or_skip(outside, checkout / "aab_plugin_echo", directory=True)
    with pytest.raises(PackageError, match="symlink"):
        read_package(checkout)
