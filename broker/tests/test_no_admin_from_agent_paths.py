"""Structural: nothing an agent can reach imports approvals or owner identity.

Walks the static import graph (AST, following relative and absolute
`broker.*` imports, plus every parent package's __init__, since importing a
submodule runs it) from the agent routers and services/agent.py, and
asserts services/admin.py and identity/* are not in it. If an agent-facing
module ever imports the approve path, this fails before any code runs.
"""

import ast
from pathlib import Path

import broker

ROOT = Path(broker.__file__).resolve().parent
AGENT_ROOTS = ("broker.routers.targets", "broker.routers.actions", "broker.routers.me",
               "broker.routers.permissions", "broker.routers.delegations",
               "broker.routers.skill", "broker.services.agent",
               "broker.services.delegation", "broker.mcp_server")


def _file(mod: str) -> Path | None:
    rel = mod.split(".")[1:]
    base = ROOT.joinpath(*rel)
    if base.with_suffix(".py").is_file():
        return base.with_suffix(".py")
    if (base / "__init__.py").is_file():
        return base / "__init__.py"
    return None


def _is_pkg(mod: str) -> bool:
    f = _file(mod)
    return f is not None and f.name == "__init__.py"


def _imports(mod: str) -> set[str]:
    path = _file(mod)
    if path is None:
        return set()
    pkg = mod if _is_pkg(mod) else mod.rsplit(".", 1)[0]
    out = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names if a.name.split(".")[0] == "broker"}
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = pkg.split(".")
                base = base[:len(base) - (node.level - 1)]
                target = ".".join(base + ([node.module] if node.module else []))
            elif node.module and node.module.split(".")[0] == "broker":
                target = node.module
            else:
                continue
            out.add(target)
            for a in node.names:         # `from . import x` may name a submodule
                if _file(f"{target}.{a.name}") is not None:
                    out.add(f"{target}.{a.name}")
    # Importing a.b.c runs a/__init__ and a/b/__init__ too.
    for m in list(out):
        parts = m.split(".")
        out |= {".".join(parts[:i]) for i in range(1, len(parts))}
    return {m for m in out if _file(m) is not None}


def graph(roots) -> set[str]:
    seen, todo = set(), list(roots)
    while todo:
        m = todo.pop()
        if m in seen:
            continue
        seen.add(m)
        todo.extend(_imports(m) - seen)
    return seen


def test_agent_surface_cannot_reach_admin_or_identity():
    g = graph(AGENT_ROOTS)
    assert "broker.engine" in g and "broker.policy" in g        # the walk is not vacuous
    assert "broker.mcp_tools" in g and "broker.mcp_generic" in g
    assert "broker.services.delegation" in g and "broker.skill.generator" in g
    assert "broker.services.admin" not in g
    assert "broker.deps" not in g
    assert not [m for m in g if m.startswith("broker.identity")], sorted(g)


def test_the_walker_does_find_admin_imports():
    # Sanity check of the walker itself: the admin router does reach identity.
    g = graph(["broker.routers.admin_ops"])
    assert "broker.services.admin" in g
    assert any(m.startswith("broker.identity") for m in g)
