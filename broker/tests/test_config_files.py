"""The files that configure a deployment agree with each other: the env-split
table is identical in docs/deployment.md and docs/architecture.md section
2.2, compose only references keys .env.example carries, and the third-party
credentials that moved to the console appear in no file."""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MOVED_TO_CONSOLE = ("TELEGRAM_BOT_TOKEN", "GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY_PATH",
                    "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET")


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


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
