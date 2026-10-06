"""The plugin base image (plugins/base/Dockerfile) and the workflows that build
and publish it, read statically: no Docker is needed to hold them to their
contract.

External plugins build FROM ghcr.io/pelegw/aab-plugin-base:<version>
(docs/plugin-packaging.md; the finance plugin's Dockerfile relies on exactly
this): Python 3.12, the aab user at uid/gid 10001 like every image in the
stack, /secrets owned by aab with mode 0700 and declared a VOLUME,
PLUGIN_SECRETS_DIR=/secrets, the in-tree plugins' TCP liveness check, port
8090, USER aab, and no CMD. release.yml publishes it with the runtime wheel
on a v* tag that equals VERSION; CI builds it on every push without pushing.
"""

import re
import tomllib
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
BASE = "plugins/base/Dockerfile"
IMAGE = "ghcr.io/pelegw/aab-plugin-base"


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def _instructions(rel: str) -> list[str]:
    """Dockerfile instructions, continuation lines joined, comments dropped."""
    out, current = [], ""
    for line in _read(rel).splitlines():
        if not current and (not line.strip() or line.lstrip().startswith("#")):
            continue
        current += line.rstrip("\\").strip() + " "
        if not line.rstrip().endswith("\\"):
            out.append(" ".join(current.split()))
            current = ""
    return out


def _workflow(rel: str) -> dict:
    doc = yaml.safe_load(_read(rel))
    doc["on"] = doc.pop(True, doc.get("on"))          # YAML 1.1 reads a bare `on` as true
    return doc


def _lines(block: str) -> list[str]:
    return [line.strip() for line in block.splitlines() if line.strip()]


def test_the_base_image_keeps_the_contract_plugins_build_on():
    ins = _instructions(BASE)
    assert ins[0] == "FROM python:3.12-slim"
    assert "WORKDIR /srv" in ins
    [user_run] = [i for i in ins if i.startswith("RUN groupadd")]
    for part in ("groupadd --system --gid 10001 aab", "useradd --system --uid 10001 --gid aab",
                 "mkdir -p /secrets", "chown aab:aab /secrets", "chmod 0700 /secrets"):
        assert part in user_run, part
    assert "VOLUME /secrets" in ins and "EXPOSE 8090" in ins
    [env] = [i for i in ins if i.startswith("ENV ")]
    for pair in ("PLUGIN_SECRETS_DIR=/secrets", "PYTHONDONTWRITEBYTECODE=1", "PYTHONUNBUFFERED=1"):
        assert pair in env, pair
    users = [i for i in ins if i.startswith("USER ")]
    assert users == ["USER aab"] and ins.index("USER aab") > ins.index(user_run)
    # The plugin names its own app factory; the base runs nothing of its own.
    assert not [i for i in ins if i.startswith(("CMD ", "ENTRYPOINT "))]


def test_the_runtime_comes_from_this_checkout_with_uvicorn():
    ins = _instructions(BASE)
    assert "COPY --from=runtime pyproject.toml ./plugin-runtime/pyproject.toml" in ins
    assert "COPY --from=runtime aab_plugin_runtime ./plugin-runtime/aab_plugin_runtime" in ins
    [pip] = [i for i in ins if "pip install" in i]
    assert "./plugin-runtime" in pip and '"uvicorn>=0.30,<1"' in pip and "rm -rf ./plugin-runtime" in pip


def test_the_liveness_check_is_the_in_tree_plugins_one():
    def check(rel):
        [hc] = [i for i in _instructions(rel) if i.startswith("HEALTHCHECK ")]
        return hc
    assert check(BASE) == check("plugins/github/Dockerfile") == check("plugins/google/Dockerfile")
    assert "8090" in check(BASE) and "X-Plugin-Token" not in check(BASE)


def test_the_runtime_is_versioned_on_the_gateway_line_it_ships_with():
    runtime = tomllib.loads(_read("plugin-runtime/pyproject.toml"))["project"]["version"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", runtime)
    assert runtime == "0.3.0"         # the external-plugins release; the base image tag follows VERSION


def test_release_publishes_the_wheel_and_the_image_on_v_tags_only():
    wf = _workflow(".github/workflows/release.yml")
    assert wf["on"] == {"push": {"tags": ["v*"]}}
    assert wf["permissions"] == {"contents": "write", "packages": "write"}
    steps = wf["jobs"]["publish"]["steps"]
    runs = "\n".join(s.get("run", "") for s in steps)
    # Nothing is published from a tag that is not v<VERSION> or from a runtime
    # on another X.Y line.
    assert 'test "${GITHUB_REF_NAME}" = "v${version}"' in runs
    assert 'test "${runtime%.*}" = "${version%.*}"' in runs
    assert "pip wheel --no-deps --wheel-dir dist ./plugin-runtime" in runs
    assert 'gh release create "${GITHUB_REF_NAME}" --verify-tag' in runs
    assert 'gh release upload "${GITHUB_REF_NAME}" dist/aab_plugin_runtime-*.whl --clobber' in runs
    [login] = [s for s in steps if str(s.get("uses", "")).startswith("docker/login-action")]
    assert login["with"] == {"registry": "ghcr.io", "username": "${{ github.actor }}",
                             "password": "${{ secrets.GITHUB_TOKEN }}"}
    [push] = [s for s in steps if str(s.get("uses", "")).startswith("docker/build-push-action")]
    w = push["with"]
    assert w["context"] == "plugins/base" and w["push"] is True
    assert _lines(w["build-contexts"]) == ["runtime=plugin-runtime"]
    assert _lines(w["tags"]) == [f"{IMAGE}:${{{{ steps.version.outputs.version }}}}",
                                 f"{IMAGE}:latest"]
    assert "org.opencontainers.image.source=https://github.com/${{ github.repository }}" in \
        w["labels"]


def test_ci_builds_the_base_image_without_pushing_and_checks_its_contract():
    ci = _workflow(".github/workflows/ci.yml")
    job = ci["jobs"]["plugin-base-image"]
    runs = "\n".join(s.get("run", "") for s in job["steps"])
    assert "docker build --build-context runtime=plugin-runtime -t aab-plugin-base:ci plugins/base" \
        in runs
    assert "docker push" not in runs and "push: true" not in _read(".github/workflows/ci.yml")
    assert 'test "$(id -u):$(id -g)" = "10001:10001"' in runs
    assert 'test "$(stat -c %u:%a /secrets)" = "10001:700"' in runs
    assert "import aab_plugin_runtime, uvicorn" in runs


def test_the_dockerfile_template_in_the_packaging_doc_builds_on_this_base():
    """docs/plugin-packaging.md's template is what plugin authors copy: it
    must start FROM the published base image, end as aab, and serve one
    worker on :8090 with uvicorn's access log off (docs/logging.md)."""
    doc = _read("docs/plugin-packaging.md")
    [block] = re.findall(r"```dockerfile\n(.*?)```", doc, re.S)
    lines = [line for line in block.splitlines() if line and not line.startswith("#")]
    assert re.fullmatch(rf"FROM {re.escape(IMAGE)}:\d+\.\d+\.\d+", lines[0])
    assert [line for line in lines if line.startswith("USER ")][-1] == "USER aab"
    [cmd] = [line for line in lines if line.startswith("CMD ")]
    for part in ('"uvicorn"', '"--factory"', '"--port", "8090"', '"--workers", "1"',
                 '"--no-access-log"'):
        assert part in cmd, part
