"""The files that configure a deployment agree with each other: the env-split
table is identical in docs/deployment.md and docs/architecture.md section
2.2, compose only references keys .env.example carries, and the third-party
credentials that moved to the console appear in no file. Compose also keeps
the isolation docs/architecture.md section 2 describes: the WhatsApp session
volume is mounted by the sidecar alone, and the archive read-only elsewhere."""

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
MOVED_TO_CONSOLE = ("TELEGRAM_BOT_TOKEN", "GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY_PATH",
                    "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET")


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


class _ComposeLoader(yaml.SafeLoader):
    """Compose's `!reset` (the public overlay) is not YAML-core; read it as
    the plain value it wraps."""


_ComposeLoader.add_constructor("!reset", lambda loader, node: (
    loader.construct_sequence(node) if isinstance(node, yaml.SequenceNode)
    else loader.construct_mapping(node) if isinstance(node, yaml.MappingNode)
    else loader.construct_scalar(node)))


def _compose(rel: str) -> dict:
    return yaml.load(_read(rel), Loader=_ComposeLoader)


def _mounts(service: dict) -> list[tuple[str, str, bool]]:
    """(source, target, read_only) for each volume entry of a service."""
    out = []
    for v in service.get("volumes", []):
        if isinstance(v, dict):
            out.append((v["source"], v["target"], bool(v.get("read_only"))))
        else:
            parts = v.split(":")
            out.append((parts[0], parts[1], len(parts) > 2 and "ro" in parts[2].split(",")))
    return out


def _env_table(text: str) -> str:
    start = text.index("| Container | Receives | Must never receive |")
    return text[start:text.index("\n\n", start)]


def test_env_split_table_is_identical_in_both_docs():
    assert _env_table(_read("docs/deployment.md")) == _env_table(_read("docs/architecture.md"))


def test_compose_references_only_keys_the_env_file_has():
    keys = {line.split("=", 1)[0] for line in _read(".env.example").splitlines()
            if line and not line.startswith("#")}
    for rel in ("docker-compose.yml", "docker-compose.public.yml"):
        used = set(re.findall(r"\$\{([A-Z0-9_]+)", _read(rel)))
        assert used, rel                                   # the scan is not vacuous
        assert used <= keys, (rel, sorted(used - keys))


def test_moved_credentials_are_in_no_deployment_file():
    for rel in (".env.example", "docker-compose.yml", "docker-compose.public.yml"):
        text = _read(rel)
        for name in MOVED_TO_CONSOLE:
            assert name not in text, (rel, name)


def test_the_whatsapp_session_volume_is_mounted_by_the_sidecar_only():
    """session.db is the WhatsApp credential: its volume is in exactly one
    service's filesystem, and the sidecar is told to keep the session there."""
    for rel in ("docker-compose.yml", "docker-compose.public.yml"):
        for name, svc in _compose(rel)["services"].items():
            for source, target, _ in _mounts(svc):
                if source == "wa_session":
                    assert (rel, name, target) == (
                        "docker-compose.yml", "whatsapp-sidecar", "/session")
    base = _compose("docker-compose.yml")
    assert "wa_session" in base["volumes"]
    sidecar = base["services"]["whatsapp-sidecar"]
    assert ("wa_session", "/session", False) in _mounts(sidecar)
    assert sidecar["environment"]["SESSION_DIR"] == "/session"


def test_the_archive_volume_is_read_only_outside_the_sidecar():
    base = _compose("docker-compose.yml")
    users = {name: [(t, ro) for s, t, ro in _mounts(svc) if s == "wa_data"]
             for name, svc in base["services"].items()}
    assert {n: m for n, m in users.items() if m} == {
        "whatsapp-sidecar": [("/data", False)], "plugin-whatsapp": [("/data", True)]}
    assert base["services"]["plugin-whatsapp"]["environment"]["MESSAGES_DB"] == \
        "/data/messages.db"
