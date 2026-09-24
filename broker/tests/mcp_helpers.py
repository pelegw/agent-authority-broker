"""Helpers for driving the live MCP endpoint in tests (JSON-RPC over
streamable HTTP through TestClient, which sends Host: testserver)."""

import json

import pytest
from fastapi.testclient import TestClient

ACCEPT = {"Accept": "application/json, text/event-stream"}


@pytest.fixture()
def live(env):
    """A TestClient with the app lifespan running (the MCP session manager
    only serves inside it)."""
    from broker.main import app
    with TestClient(app) as c:
        yield c


def rpc(client, headers, method, params=None, rid=1, expect=200) -> dict:
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": rid, "method": method,
                                  "params": params or {}},
                    headers={**headers, **ACCEPT})
    assert r.status_code == expect, r.text
    return r.json()


def tools(client, headers) -> dict:
    """{name: tool dict} from a live tools/list."""
    body = rpc(client, headers, "tools/list")
    return {t["name"]: t for t in body["result"]["tools"]}


def call(client, headers, name, arguments=None) -> dict:
    """The CallToolResult dict of a live tools/call."""
    body = rpc(client, headers, "tools/call", {"name": name, "arguments": arguments or {}})
    return body["result"]


def text_json(result: dict):
    """The JSON inside a single text content block."""
    [block] = result["content"]
    assert block["type"] == "text"
    return json.loads(block["text"])
